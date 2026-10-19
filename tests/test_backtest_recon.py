"""E7.22 (D125): live <-> sim reconciliation by replay (``arc.backtest.recon``).

A fixture store (migrated schema, hand-written live rows) and a synthetic EOD history
(``tests.backtest_data_fixtures``) cover the four sections: the slippage math, the
nearest-structure matching, the exit replay through the sim's own ``exit_walk``, and
the stance join -- plus the routine handler and the CLI end to end.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from arc.backtest import recon as R
from arc.backtest.costs import load_cost_model
from arc.backtest.data import DataSettings, HistoryView
from arc.backtest.ranking import load_ranking_file
from arc.backtest.regime import label_trend_v2
from arc.backtest.strategies import StrategyKind
from arc.config import ArcSettings
from arc.exits.policy import load_exit_config
from arc.scanner.rank import RankFilters
from arc.utils.calendar import ET
from tests.backtest_data_fixtures import (
    R as RATE,
)
from tests.backtest_data_fixtures import (
    sessions,
    spot_path,
    synth_rows,
    write_history,
    write_underlying,
    write_universe,
)

REPO = Path(__file__).resolve().parent.parent
COST = load_cost_model(REPO / "config" / "costs.yaml")
EXITS = load_exit_config()
N = 70
DAYS = sessions(dt.date(2025, 3, 3), N)
ENTRY, EXIT, REGATE = DAYS[30], DAYS[33], DAYS[36]
NOW = dt.datetime.combine(DAYS[-1], dt.time(10, 0), tzinfo=ET)


def _cfg() -> Any:
    cfg = load_ranking_file()
    bt = cfg.backtest.model_copy(update={"n_paths": 200})
    return cfg.model_copy(
        update={
            "backtest": bt,
            "ranking": cfg.ranking.model_copy(update={"filters": RankFilters(enabled=False)}),
        }
    )


CFG = _cfg()
SETTINGS = ArcSettings().with_profile("margin")


@pytest.fixture(scope="module")
def view(tmp_path_factory: pytest.TempPathFactory) -> HistoryView:
    root = tmp_path_factory.mktemp("recon-data")
    spots = spot_path(N)
    for t in ("AAA", "SPY"):
        write_history(root, t, synth_rows(t, DAYS, spots, oi=400.0))
        write_underlying(root, t, DAYS, spots)
    q = dt.date(2025, 1, 1)
    write_universe(root, ["AAA", "SPY"], [(q, "AAA", True), (q, "SPY", True)])
    return HistoryView(
        root,
        settings=DataSettings(pit=False),
        universe="c125",
        start=DAYS[0],
        end=DAYS[-1],
        cost=COST,
        flat_rate=RATE,
        min_iv_obs=20,
    )


def _menu(view: HistoryView, day: dt.date) -> list[Any]:
    h = view.load("AAA")
    return R._sim_menu(
        h,
        "AAA",
        day,
        profile="margin",
        settings=SETTINGS,
        cfg=CFG,
        exits=EXITS,
        cost=COST,
        rate=view.rates.rate(day),
    )


def _long_call(view: HistoryView) -> Any:
    return next(c for c in _menu(view, ENTRY) if c.kind == StrategyKind.LONG_CALL)


def _eod(view: HistoryView, day: dt.date, symbol: str) -> tuple[float, float]:
    raw = view.load("AAA").raw_day(day)
    row = raw[raw["symbol"] == symbol].iloc[0]
    return float(row["bid"]), float(row["ask"])


# ---------------------------------------------------------------------------
# Fixture store
# ---------------------------------------------------------------------------


def _utc(day: dt.date, hh: int, mm: int = 0) -> str:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=ET).astimezone(dt.UTC).isoformat()


def _structure(sym: str, side: str, premium: float, sleeve: str = "short") -> str:
    leg = {"occ_symbol": sym, "side": side, "ratio": 1, "premium": premium}
    return json.dumps({"kind": "long_call", "sleeve": sleeve, "legs": [leg]})


def _proposal(c: sqlite3.Connection, h: str, day: dt.date, kind: str, st: str) -> None:
    c.execute(
        "INSERT INTO proposals (candidate_id, proposal_hash, structure_json, thesis, quant_json,"
        " sizing_json, expires_at, created_at, day, ticker, kind, regime)"
        " VALUES ('cand-1', ?, ?, 't', '{}', '{}', ?, ?, ?, 'AAA', ?, 'risk_on')",
        (h, st, _utc(day, 16), _utc(day, 10), day.isoformat(), kind),
    )


def _execution(
    c: sqlite3.Connection,
    h: str,
    *,
    kind: str,
    day: dt.date,
    fill: float,
    band: tuple[float, float],
    steps: int,
    detail: str = "",
    structure_id: str | None = None,
) -> None:
    c.execute(
        "INSERT INTO executions (proposal_hash, kind, structure_id, status, token_version,"
        " band_lo, band_hi, max_steps, steps_used, contracts, filled_qty, fill_price, detail,"
        " started_at, finished_at)"
        " VALUES (?, ?, ?, 'filled', 'arc2', ?, ?, 4, ?, 1, 1, ?, ?, ?, ?)",
        (h, kind, structure_id, str(band[0]), str(band[1]), steps, str(fill), detail,
         _utc(day, 11), _utc(day, 11, 5)),
    )  # fmt: skip
    c.execute(
        "INSERT INTO orders (id, proposal_hash, client_order_id, state, created_at, updated_at)"
        " VALUES (?, ?, ?, 'filled', ?, ?)",
        (f"o-{h}", h, f"c-{h}", _utc(day, 11), _utc(day, 11, 5)),
    )
    c.execute(
        "INSERT INTO fills (id, order_id, qty, price, filled_at) VALUES (?, ?, 1, ?, ?)",
        (f"f-{h}", f"o-{h}", str(fill), _utc(day, 11, 4)),
    )


def _decision(c: sqlite3.Connection, i: str, **kw: Any) -> None:
    row = {"persona": "research", "stage": "shortlist", "subject": "AAA", "choice": "selected",
           "reason_code": "shortlisted", "payload": "{}", "proposal_hash": None} | kw  # fmt: skip
    c.execute(
        "INSERT INTO decisions (id, persona, stage, subject, choice, reason_code, payload,"
        " proposal_hash, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (i, row["persona"], row["stage"], row["subject"], row["choice"], row["reason_code"],
         row["payload"], row["proposal_hash"], row["at"]),
    )  # fmt: skip


def build_store(path: Path, sym: str, *, entry_q: tuple[float, float]) -> dict[str, float]:
    """One closed long call (open + close fills), one re-gated open, two stances."""
    from arc.store.db import connect
    from arc.store.migrate import migrate

    c = connect(path)
    migrate(c)
    c.execute(
        "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at)"
        " VALUES ('cand-1', 'AAA', 'bullish', 'none', 0.6, ?)",
        (_utc(ENTRY, 9),),
    )
    bid, ask = entry_q
    mid = (bid + ask) / 2
    open_fill = round(mid + 0.25 * (ask - bid), 4)  # x = 0.25 by construction
    _proposal(c, "h-open", ENTRY, "open", _structure(sym, "long", mid))
    _execution(c, "h-open", kind="open", day=ENTRY, fill=open_fill, band=(mid, ask), steps=2)
    c.execute(
        "INSERT INTO market_contexts (id, proposal_hash, payload, quotes_as_of, created_at)"
        " VALUES ('mc-1', 'h-open', ?, ?, ?)",
        (json.dumps({"legs": [{"occ_symbol": sym, "bid": bid, "ask": ask}]}),
         _utc(ENTRY, 10, 59), _utc(ENTRY, 11)),
    )  # fmt: skip
    # the close: sells the call at its review mid less 0.1 of the D24 band width
    close_mid, lo, hi = 3.0, -3.0, -2.9
    close_fill = -2.99
    _proposal(c, "h-close", EXIT, "close", _structure(sym, "short", close_mid))
    _execution(
        c, "h-close", kind="close", day=EXIT, fill=close_fill, band=(lo, hi), steps=1,
        structure_id="os-1",
    )  # fmt: skip
    pnl = round(-(open_fill + close_fill) * 100.0, 2)
    c.execute(
        "INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id,"
        " structure_json, contracts, entry_net, opened_at, status, exit_proposal_hash,"
        " exit_reason, exit_day, closed_at, close_net)"
        " VALUES ('os-1', 'AAA', 'h-open', 'cand-1', ?, 1, ?, ?, 'closed', 'h-close',"
        " 'research_review', ?, ?, ?)",
        (_structure(sym, "long", mid), str(open_fill), _utc(ENTRY, 11, 4), EXIT.isoformat(),
         _utc(EXIT, 11, 4), str(close_fill)),
    )  # fmt: skip
    c.execute(
        "INSERT INTO outcomes (id, proposal_hash, status, realised_pnl, exit_reason, at)"
        " VALUES ('out-1', 'h-open', 'closed', ?, 'research_review', ?)",
        (str(pnl), _utc(EXIT, 11, 5)),
    )
    # a re-gated open (D101): priced on the band_moved decision's mid / far touch
    _proposal(c, "h-regate", REGATE, "open", _structure(sym, "long", 2.0))
    _execution(
        c, "h-regate", kind="open", day=REGATE, fill=2.05, band=(2.0, 2.1), steps=3,
        detail="re-priced after band move",
    )  # fmt: skip
    _decision(
        c, "d-rg", persona="broker", stage="order", reason_code="order:band_moved",
        payload=json.dumps({"mid": 2.0, "far_touch": 2.1}), proposal_hash="h-regate",
        at=_utc(REGATE, 10, 59),
    )  # fmt: skip
    _decision(c, "d-s1", payload=json.dumps({"stance": "neutral"}), at=_utc(ENTRY, 9))
    _decision(c, "d-s2", payload=json.dumps({"stance": "bullish"}), at=_utc(ENTRY, 9, 30))
    _decision(c, "d-s3", payload=json.dumps({"stance": "bearish"}), at=_utc(EXIT, 9))
    c.commit()
    c.close()
    return {"open_fill": open_fill, "close_fill": close_fill, "pnl": pnl, "close_mid": close_mid}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, view: HistoryView) -> dict[str, Any]:
    db = tmp_path_factory.mktemp("recon-store") / "arc.db"
    cand = _long_call(view)
    sym = cand.picks[0].symbol
    facts = build_store(db, sym, entry_q=(4.9, 5.1))
    rep = R.run_recon(
        db, data_dir=db.parent, now=NOW, view=view, cfg=CFG, exits=EXITS, cost=COST,
        settings=SETTINGS, git_sha="test",
    )  # fmt: skip
    return {"db": db, "sym": sym, "cand": cand, "rep": rep, **facts}


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def test_combo_nbbo_is_the_signed_leg_sum() -> None:
    legs = [
        R.LegQuote(symbol="AAA250404C00100000", side=1, bid=2.0, ask=2.2),
        R.LegQuote(symbol="AAA250404C00105000", side=-1, bid=0.9, ask=1.0),
    ]
    mid, spread = R.combo_nbbo(legs)  # type: ignore[misc]
    assert mid == pytest.approx(2.1 - 0.95)
    assert spread == pytest.approx(0.2 + 0.1)
    assert R.combo_nbbo([legs[0].model_copy(update={"bid": None})]) is None
    assert R.combo_nbbo([legs[0].model_copy(update={"bid": 3.0})]) is None  # crossed


@pytest.mark.parametrize(
    ("fill", "x"),
    [(5.05, 0.25), (5.0, 0.0), (4.95, -0.25), (-2.95, 0.25), (-3.05, -0.25)],
)
def test_realised_x_pay_convention(fill: float, x: float) -> None:
    """+ = cost: paying above mid on a debit, receiving less than mid on a credit."""
    mid = 5.0 if fill > 0 else -3.0
    assert R.realised_x(fill, mid, 0.2) == pytest.approx(x)


def test_realised_x_needs_a_spread() -> None:
    assert R.realised_x(1.0, 1.0, 0.0) is None
    assert R.realised_x(1.0, None, 0.1) is None


def test_estimate_x_reports_n_ci_and_insufficient() -> None:
    one = R.estimate_x([0.2])
    assert (one.n, one.mean, one.insufficient, one.lo) == (1, 0.2, True, None)
    tight = R.estimate_x([0.2, 0.21, 0.19, 0.2, 0.22, None])
    assert tight.n == 5 and not tight.insufficient
    assert tight.lo is not None and tight.hi is not None and tight.lo <= 0.204 <= tight.hi
    wide = R.estimate_x([-1.0, 1.5, -0.8, 2.0])
    assert wide.insufficient  # CI half-width > 0.25 x
    assert R.estimate_x([0.2, 0.3]) == R.estimate_x([0.2, 0.3])  # seeded


def test_tod_and_period_buckets() -> None:
    d = dt.date(2026, 10, 14)
    at = [dt.time(9, 45), dt.time(12, 0), dt.time(15, 0)]
    got = [R.tod_bucket(dt.datetime.combine(d, t, tzinfo=ET)) for t in at]
    assert got == ["open", "midday", "late"]
    assert R.period_of("margin") == "post_switch"
    assert R.period_of("cash_debit") == "pre_switch"
    assert R.period_of(None) == "pre_switch"


def test_live_kind_maps_verticals_by_right() -> None:
    call = R.LegQuote(symbol="AAA250404C00100000", side=1)
    put = R.LegQuote(symbol="AAA250404P00100000", side=1)
    assert R.live_kind("vertical_debit", [call, call]) == StrategyKind.BULL_CALL
    assert R.live_kind("vertical_debit", [put, put]) == StrategyKind.BEAR_PUT
    assert R.live_kind("vertical_credit", [call, call]) == StrategyKind.BEAR_CALL
    assert R.live_kind("vertical_credit", [put, put]) == StrategyKind.BULL_PUT
    assert R.live_kind("long_call", [call]) == StrategyKind.LONG_CALL
    assert R.live_kind("vertical_debit", [call, put]) is None
    assert R.live_kind("calendar", [call]) is None


def test_measure_override_beats_the_sha_default() -> None:
    d0, d1, d2 = dt.date(2026, 10, 12), dt.date(2026, 10, 14), dt.date(2026, 10, 16)
    over = [(d1, "managed_net_ev"), (d2, None)]  # set, then reverted

    def at_sha(sha: str | None) -> str:
        return f"yaml@{sha}"

    assert R._measure_for(d0, "a", over, at_sha) == "yaml@a"
    assert R._measure_for(d1, "a", over, at_sha) == "managed_net_ev"
    assert R._measure_for(d2, "b", over, at_sha) == "yaml@b"
    assert R._measure_for(d2, None, over, None) is None


# ---------------------------------------------------------------------------
# Nearest-structure matching
# ---------------------------------------------------------------------------


def test_match_exact_same_expiry_nearest_and_absent(view: HistoryView) -> None:
    menu = _menu(view, ENTRY)
    cand = next(c for c in menu if c.kind == StrategyKind.LONG_CALL)
    sym = cand.picks[0].symbol
    spot = float(view.load("AAA").closes[ENTRY])
    live = [R.LegQuote(symbol=sym, side=1)]
    m, c, dd, ds = R.match_structure(live, StrategyKind.LONG_CALL, menu, spot=spot)
    assert (m, c, dd, ds) == ("exact", cand, 0, 0.0)
    # same expiry, a strike off the menu: nearest same-kind candidate, strike gap / spot
    occ = sym[:-8] + f"{int((cand.picks[0].strike + 0.5) * 1000):08d}"
    m, c, dd, ds = R.match_structure(
        [R.LegQuote(symbol=occ, side=1)], StrategyKind.LONG_CALL, menu, spot=spot
    )
    assert m == "same_expiry" and dd == 0 and c is not None
    assert ds == pytest.approx(min(abs(p.picks[0].strike - (cand.picks[0].strike + 0.5))
                                   for p in menu if p.kind == StrategyKind.LONG_CALL
                                   and p.picks[0].expiration == cand.picks[0].expiration)
                               / spot)  # fmt: skip
    # an expiry the menu lacks: nearest by days first
    far = (cand.picks[0].expiration + dt.timedelta(days=3)).strftime("%y%m%d")
    occ2 = sym[:3] + far + sym[9:]
    m, _, dd, _ = R.match_structure(
        [R.LegQuote(symbol=occ2, side=1)], StrategyKind.LONG_CALL, menu, spot=spot
    )
    assert m == "nearest" and dd is not None and dd > 0
    assert R.match_structure(live, None, menu, spot=spot)[0] == "kind_absent"
    assert R.match_structure(live, StrategyKind.IRON_CONDOR, [], spot=spot)[0] == "kind_absent"


def test_leg_key_is_the_sim_candidate_key(view: HistoryView) -> None:
    cand = _long_call(view)
    key = R.leg_key(cand.kind, [(p.symbol, p.side) for p in cand.picks])
    assert key.startswith("long_call|+1AAA")


# ---------------------------------------------------------------------------
# End to end on the fixture store
# ---------------------------------------------------------------------------


def test_slippage_rows_name_their_nbbo_source(world: dict[str, Any], view: HistoryView) -> None:
    rep = world["rep"]
    by = {r.proposal_hash: r for r in rep.slippage}
    assert set(by) == {"h-open", "h-close", "h-regate"}
    op = by["h-open"]
    assert (op.side, op.nbbo_source, op.step, op.kind, op.sleeve) == (
        "open", "market_contexts", 2, "long_call", "short",
    )  # fmt: skip
    assert op.x == pytest.approx(0.25) and op.mid == pytest.approx(5.0)
    bid, ask = _eod(view, ENTRY, world["sym"])
    assert op.x_eod == pytest.approx((world["open_fill"] - (bid + ask) / 2) / (ask - bid), abs=1e-3)
    cl = by["h-close"]
    # closes: the review mid (sold: -3.0) and twice the D24 band half-width / reach
    reach = float(SETTINGS.execution_band_reach)
    assert cl.nbbo_source == "band" and cl.mid == pytest.approx(-3.0)
    assert cl.spread == pytest.approx(2 * 0.1 / reach, abs=1e-4)
    assert cl.x == pytest.approx((-2.99 + 3.0) / (0.2 / reach), abs=1e-3)
    rg = by["h-regate"]
    assert (rg.nbbo_source, rg.mid, rg.spread) == ("regate", 2.0, 0.2)
    assert rg.x == pytest.approx(0.25)
    assert all(r.arm == "control" and r.asset == "stock" for r in rep.slippage)
    assert {r.tod for r in rep.slippage} == {"midday"}


def test_overlap_finds_the_exact_pick_and_ranks_it(world: dict[str, Any]) -> None:
    rows = {r.day: r for r in world["rep"].overlap}
    assert set(rows) == {ENTRY, REGATE}
    r = rows[ENTRY]
    assert r.match == "exact" and r.live_kind == "long_call" and r.menu_size > 0
    assert r.live_key == world["cand"].inputs.key
    assert r.live_passes_filters is True  # filters off in the fixture config
    assert r.ranks and any(v is not None for v in r.ranks.values())
    assert r.note is not None and "live stance bullish" in r.note


def test_exit_replay_runs_the_sim_exit_walk(world: dict[str, Any]) -> None:
    (r,) = world["rep"].exits
    assert (r.ticker, r.kind, r.entry_day, r.live_exit_day) == ("AAA", "long_call", ENTRY, EXIT)
    assert r.live_pnl == pytest.approx(world["pnl"])
    assert r.entry_sim is not None and r.close_sim_at_live_day is not None
    assert r.sim_reason is not None and r.proxy_reason is not None
    assert r.sim_pnl is not None and r.sim_exit_day is not None and r.sim_exit_day > EXIT
    assert r.tracking_error == pytest.approx(world["pnl"] - r.sim_pnl, abs=0.01)
    # the decomposition is exact: err = pricing gap + timing gap
    assert r.pricing_gap is not None and r.timing_gap is not None
    assert r.pricing_gap + r.timing_gap == pytest.approx(r.tracking_error, abs=0.02)
    # live paid 5.05 for a call the EOD book prices near 1.1: almost all of err is pricing
    assert abs(r.pricing_gap) > abs(r.timing_gap)


def test_exit_replay_matches_the_sim_on_its_own_entry(view: HistoryView) -> None:
    """A live trade entered at the sim's own fill replays to the sim's own outcome."""
    from arc.backtest.engine import open_trade
    from arc.backtest.sim import exit_walk
    from arc.backtest.strategies import StrategySpec
    from arc.exits.policy import resolve_rules
    from arc.structures.analytics import analyze

    cand = _long_call(view)
    h = view.load("AAA")
    base = COST.model_copy(update={"slippage_frac": CFG.backtest.sim.slippage_x})
    o = open_trade(
        cand.picks, spec=StrategySpec(kind=StrategyKind.LONG_CALL), underlying="AAA",
        entry_date=ENTRY, spot=float(h.closes[ENTRY]), cost=base,
    )  # fmt: skip
    assert o is not None
    from arc.backtest.engine import STRUCTURE_KIND
    from arc.backtest.ranking import _legs

    rules = resolve_rules(
        analyze(_legs(cand.picks, "AAA"), as_of=ENTRY),
        EXITS.policy_for(
            STRUCTURE_KIND[StrategyKind.LONG_CALL], SETTINGS.exit_block_for_dte(o.dte)
        ),
    )
    hist = R._History(view)
    pos = R.LivePosition(
        arm="control", structure_id="x", ticker="AAA", kind="long_call", sleeve="short",
        profile="margin", regime=None,
        legs=[R.LegQuote(symbol=cand.picks[0].symbol, side=1)], contracts=1,
        entry_day=ENTRY, entry_fill=1.0, exit_day=EXIT, close_fill=None,
        exit_reason="stop", realised_pnl=None,
    )  # fmt: skip
    (row,) = R.exit_rows(
        [pos], hist, cfg=CFG, exits=EXITS, cost=COST, settings_for=lambda _p: SETTINGS,
        default_profile="margin",
    )  # fmt: skip
    trade, _, _ = exit_walk(
        o, rules, entry_day=ENTRY, book=hist.marks("AAA", CFG.backtest.data.chain_dte_max),
        closes=h.closes, inside=[], settings=SETTINGS,
        spread_rule=CFG.backtest.sim.spread_rule == "live", cost=base, fill_cost=None,
        ranges=None, rate=hist.rate,
    )  # fmt: skip
    assert trade is not None
    assert row.sim_reason == trade.exit_reason
    assert row.sim_exit_day == (trade.exit_date or trade.expiration)
    assert row.sim_pnl == pytest.approx(round(trade.pnl, 2))


def test_stance_join_keeps_the_last_research_stance_per_day(
    world: dict[str, Any], view: HistoryView
) -> None:
    rows = {r.day: r for r in world["rep"].stance}
    assert set(rows) == {ENTRY, EXIT}
    assert rows[ENTRY].llm == "bullish"  # 09:30 beats 09:00
    assert rows[EXIT].llm == "bearish"
    lab = label_trend_v2(view.load("AAA").closes.sort_index())
    want = {"bull": "bullish", "bear": "bearish", "sideways": "neutral"}
    assert rows[ENTRY].trend_v2 == want.get(str(lab[ENTRY]), "unknown")
    assert rows[ENTRY].m1_direction == "unknown"
    assert world["rep"].m1_note.startswith("unavailable")


def test_stance_rows_join_m1_when_supplied() -> None:
    idx = sessions(dt.date(2024, 1, 2), 120)
    closes = pd.Series([100 * 1.01**i for i in range(120)], index=idx)
    s = [R.LiveStance(arm="control", day=idx[-1], ticker="UP", stance="bullish")]
    (row,) = R.stance_rows(
        s, lambda _t: closes, asset_of=lambda _t: "stock", m1={(idx[-1], "UP"): 1.0}
    )
    assert row.m1_direction == "bullish"
    agree = R.stance_agreement([row])
    assert agree["m1_direction"]["agree"] == 1.0


def test_report_outputs_and_the_analyst_slot(world: dict[str, Any], tmp_path: Path) -> None:
    rep = world["rep"]
    paths = R.write_outputs(
        rep,
        report_md=tmp_path / "live-sim-recon.md",
        reconciliation_json=tmp_path / "studies" / "reconciliation.json",
        out_dir=tmp_path / "recon",
    )
    md = paths["report"].read_text()
    for part in (
        "## 1. Slippage",
        "## 2. Candidate overlap",
        "## 3. Exit timing",
        "## 4. Stance gap",
        "paper_bias",
        "### Stocks",
        "### ETFs",
        "insufficient",
    ):
        assert part in md  # fmt: skip
    doc = json.loads(paths["reconciliation"].read_text())
    assert doc["sim_slippage_frac"] == CFG.backtest.sim.slippage_x
    (row,) = doc["rows"]
    assert (row["kind"], row["regime"], row["n"]) == ("long_call", "risk_on", 1)
    assert any(s.startswith("x_eod per kind") for s in doc["summary"])
    # the Analyst's existing drift slot reads it unchanged (E7.15: one slot, not two)
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "arc_analyst_e722", REPO / "hermes/analyst/arc_analyst.py"
    )
    aa = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["arc_analyst_e722"] = aa
    spec.loader.exec_module(aa)  # type: ignore[union-attr]
    lines = aa.drift_lines(paths["reconciliation"])
    assert any("long_call · risk_on n=1" in ln for ln in lines)
    assert any("E7.22 live<->sim recon" in ln for ln in lines)
    series = json.loads(paths["llm"].read_text())
    assert series["rows"]  # the live_llm stance series E7.20 can read
    detail = R.ReconReport.model_validate_json(paths["detail"].read_text())
    assert detail == rep


def test_report_is_deterministic(world: dict[str, Any], view: HistoryView) -> None:
    again = R.run_recon(
        world["db"], data_dir=world["db"].parent, now=NOW, view=view, cfg=CFG, exits=EXITS,
        cost=COST, settings=SETTINGS, git_sha="test",
    )  # fmt: skip
    assert R.render_markdown(again) == R.render_markdown(world["rep"])


def test_recon_never_writes_the_store(world: dict[str, Any], view: HistoryView) -> None:
    before = world["db"].read_bytes()
    R.run_recon(
        world["db"], data_dir=world["db"].parent, now=NOW, view=view, cfg=CFG, exits=EXITS,
        cost=COST, settings=SETTINGS,
    )  # fmt: skip
    assert world["db"].read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        R._ro(world["db"]).execute("DELETE FROM candidates")


def test_since_filters_every_section(world: dict[str, Any], view: HistoryView) -> None:
    rep = R.run_recon(
        world["db"], data_dir=world["db"].parent, now=NOW, since=REGATE, view=view, cfg=CFG,
        exits=EXITS, cost=COST, settings=SETTINGS,
    )  # fmt: skip
    assert [r.proposal_hash for r in rep.slippage] == ["h-regate"]
    assert [r.day for r in rep.overlap] == [REGATE]
    assert rep.exits == [] and rep.stance == []


def test_arm_all_tags_the_arm_stores(
    tmp_path: Path, world: dict[str, Any], view: HistoryView
) -> None:
    """D69: the experiment arms' stores are read too, every row tagged by arm."""
    from arc.store.db import connect

    control = tmp_path / "arc.db"
    control.write_bytes(world["db"].read_bytes())
    arm = tmp_path / "arm-t1.db"
    build_store(arm, world["sym"], entry_q=(4.8, 5.2))
    c = connect(control)
    c.execute(
        "INSERT INTO routine_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("experiment_arm:XP-9:t1", str(arm), _utc(ENTRY, 8)),
    )
    c.commit()
    c.close()
    rep = R.run_recon(
        control, data_dir=tmp_path, now=NOW, arm="all", view=view, cfg=CFG, exits=EXITS,
        cost=COST, settings=SETTINGS,
    )  # fmt: skip
    assert rep.stores == ["XP-9:t1", "control"]
    arms = {r.arm for r in rep.slippage}
    assert arms == {"control", "XP-9:t1"}
    assert len(rep.slippage) == 6 and len(rep.exits) == 2
    solo = R.run_recon(
        control, data_dir=tmp_path, now=NOW, view=view, cfg=CFG, exits=EXITS, cost=COST,
        settings=SETTINGS,
    )  # fmt: skip
    assert solo.stores == ["control"]


# ---------------------------------------------------------------------------
# Routine + CLI
# ---------------------------------------------------------------------------


def test_routine_is_registered_weekly_background() -> None:
    from arc.routines.config import DEFAULT_ROUTINES_PATH, Lane, Weekday, load_routines
    from arc.routines.handlers import BUILTIN_HANDLERS as HANDLERS

    r = load_routines(DEFAULT_ROUTINES_PATH)
    _, spec = r.step("backtest.recon")
    assert HANDLERS["backtest.recon"] == "arc.routines.handlers:backtest_recon_source"
    assert spec.lane is Lane.BACKGROUND
    assert spec.days == [Weekday.SAT] and spec.schedule == [dt.time(10, 0)]  # type: ignore[union-attr]
    assert spec.ttl is not None and spec.ttl.duration == dt.timedelta(hours=2)  # type: ignore[union-attr]
    assert spec.options["arm"] == "all"
    assert spec.options["reconciliation"] == "docs/RESEARCH/studies/reconciliation.json"


def test_routine_handler_writes_the_report(
    tmp_path: Path, world: dict[str, Any], view: HistoryView
) -> None:
    from arc.context.store import ContextStore
    from arc.routines.config import RoutinesConfig
    from arc.routines.handlers import JobContext, RunEnv, backtest_recon_source
    from arc.store.db import connect

    db = tmp_path / "arc.db"
    db.write_bytes(world["db"].read_bytes())
    conn = connect(db)
    routines = RoutinesConfig.model_validate(
        {"sources": {"backtest.recon": {"schedule": ["10:00"], "days": ["sat"], "writes": [],
                                        "lane": "background", "arm": "all"}}}
    )  # fmt: skip
    kind, spec = routines.step("backtest.recon")
    ctx = JobContext(
        job="backtest.recon", kind=kind, spec=spec, run_id="r1", chain_run_id=None,
        scheduled_for=NOW, now=NOW, conn=conn, snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines, settings_factory=lambda: SETTINGS, clock_fn=lambda: NOW,
        run_env=RunEnv(slack=False),
    )  # fmt: skip
    res = backtest_recon_source(ctx, repo_root=tmp_path, view=view)
    assert res.metrics == {"fills": 3, "proposals": 2, "closed": 1, "stances": 2, "stores": 1}
    assert "3 fills" in res.summary
    assert (tmp_path / "docs/RESEARCH/live-sim-recon.md").exists()
    assert (tmp_path / "docs/RESEARCH/studies/reconciliation.json").exists()
    assert (tmp_path / "data/backtest/recon/recon.json").exists()
    conn.close()


def test_routine_handler_rejects_an_unknown_arm(tmp_path: Path) -> None:
    from arc.context.store import ContextStore
    from arc.routines.config import RoutinesConfig
    from arc.routines.handlers import JobContext, backtest_recon_source
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    routines = RoutinesConfig.model_validate(
        {"sources": {"backtest.recon": {"schedule": ["10:00"], "writes": [], "arm": "t1"}}}
    )
    kind, spec = routines.step("backtest.recon")
    ctx = JobContext(
        job="backtest.recon", kind=kind, spec=spec, run_id="r1", chain_run_id=None,
        scheduled_for=NOW, now=NOW, conn=conn, snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines, settings_factory=lambda: SETTINGS,
    )  # fmt: skip
    with pytest.raises(ValueError, match="arm must be control or all"):
        backtest_recon_source(ctx, repo_root=tmp_path)


def test_cli_parses_and_refuses_a_missing_store(tmp_path: Path) -> None:
    import argparse

    from arc.backtest.cli import add_backtest_parser, run_backtest_cli

    p = argparse.ArgumentParser()
    add_backtest_parser(p.add_subparsers(dest="cmd"))
    args = p.parse_args(
        ["backtest", "recon", "--db", str(tmp_path / "nope.db"), "--since", "2026-10-12",
         "--arm", "all"]
    )  # fmt: skip
    assert (args.arm, args.since) == ("all", dt.date(2026, 10, 12))
    assert run_backtest_cli(args) == 2
