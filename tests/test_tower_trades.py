"""E8.7b control tower v2 Trades: list, filters, search, per-trade drill-down, perf."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import time
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc.journal.reasons import REASON_LABELS, ReasonCode, reason_label
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from arc.tower.data import connect_ro
from arc.tower.data_trades import (
    SearchResponse,
    TradeDetail,
    TradeFilterOptions,
    TradeFilters,
    TradeListResponse,
    client_order_ref,
    date_range,
    load_filter_options,
    load_trade,
    load_trades,
    search,
)
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 28, 15, 40, tzinfo=ET)  # Monday


def _load(name: str, path: Path):  # noqa: ANN202 - module loaded by path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _load("tower_fixture_db", REPO / "scripts" / "tower_fixture_db.py")
synthetic = _load("tower_synthetic", REPO / "tests" / "tower_synthetic.py")
H = fixture.phash


@pytest.fixture(scope="module")
def fx_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("fx") / "arc.db", NOW)


@pytest.fixture
def conn(fx_db: Path):
    c = connect_ro(fx_db)
    yield c
    c.close()


@pytest.fixture
def client(fx_db: Path):
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as c:
        yield c


def _list(conn: sqlite3.Connection, **kw) -> TradeListResponse:  # noqa: ANN003
    page = kw.pop("page", 1)
    sort = kw.pop("sort", "time")
    direction = kw.pop("direction", "desc")
    size = kw.pop("size", 50)
    return load_trades(
        conn, TradeFilters(**kw), now=NOW, page=page, size=size, sort=sort, direction=direction
    )


def _detail(conn: sqlite3.Connection, tag: str) -> TradeDetail:
    d = load_trade(conn, H(tag), now=NOW)
    assert d is not None
    return d


# ---------------------------------------------------------------------------
# reason labels
# ---------------------------------------------------------------------------


def test_every_reason_code_has_a_plain_label() -> None:
    assert set(REASON_LABELS) == set(ReasonCode)
    assert all(v and v[0].isupper() for v in REASON_LABELS.values())
    assert reason_label("gate:spread_too_wide") == "Gate: bid-ask spread too wide"
    assert reason_label("made_up:thing_here") == "Made up: thing here"  # unknown: humanised


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_every_stage_and_default_sort(conn: sqlite3.Connection) -> None:
    r = _list(conn)
    assert r.total == 15 and len(r.items) == 15 and r.page == 1 and r.size == 50
    times = [i.created_at for i in r.items]
    assert times == sorted(times, reverse=True)  # type: ignore[type-var]
    stage = {(i.ticker, i.kind): i.stage for i in r.items}
    assert stage == {
        ("GOOGL", "open"): "proposed",
        ("MSFT", "open"): "gate_pass",
        ("QQQ", "close"): "gate_pass",
        ("NVDA", "open"): "open",
        ("IWM", "open"): "cancelled",
        ("AMZN", "open"): "expired",
        ("TSLA", "open"): "rejected",
        ("META", "open"): "approved",
        ("AAPL", "open"): "gate_fail",
        ("XLE", "open"): "expired",
        ("AMD", "close"): "filled",
        ("QQQ", "open"): "open",
        ("DIA", "open"): "gate_fail",
        ("SPY", "open"): "open",
        ("AMD", "open"): "closed",
    }


def test_list_row_columns(conn: sqlite3.Connection) -> None:
    by = {(i.ticker, i.kind): i for i in _list(conn).items}
    amd = by[("AMD", "open")]
    assert amd.realized_pnl == pytest.approx(300.0) and amd.exit_reason == "reallocate"
    assert amd.fill_price == D("3.60") and amd.slippage_bps == pytest.approx(0.0)
    assert amd.structure_kind == "vertical_debit" and len(amd.legs) == 2
    aapl = by[("AAPL", "open")]
    assert aapl.gate_passed is False and aapl.violations == 2
    assert aapl.first_violation is not None and aapl.first_violation.startswith(
        "per_underlying_limit:"
    )
    assert aapl.net_ev == pytest.approx(-3.2 * 4)
    spy = by[("SPY", "open")]
    # full analytics: managed vs hold-to-expiry PoP from the real exit model
    assert spy.pop_managed is not None and spy.pop_hold is not None
    assert spy.account_profile == "cash_debit" and spy.chain_run_id == "chain-fx-spy"
    assert spy.limit == D("4.15")
    close = by[("AMD", "close")]
    assert close.closes_structure_id is not None and close.swap_id == "swap-fx-amd-xle"
    assert close.limit is not None and close.limit < 0  # a credit to close


def test_summary_is_over_the_filter_not_the_page(conn: sqlite3.Connection) -> None:
    full = _list(conn)
    paged = _list(conn, size=2, page=2)
    assert paged.total == 15 and len(paged.items) == 2 and paged.summary == full.summary
    s = full.summary
    assert s.count == 15 and s.filled == 5 and s.filled_pct == pytest.approx(5 / 15)
    assert s.realized_pnl == pytest.approx(300.0) and s.realized_count == 1
    assert s.net_ev_realized == pytest.approx(12.4 * 2)  # AMD's modelled EV x contracts
    assert [i.proposal_hash for i in paged.items] == [i.proposal_hash for i in full.items[2:4]]
    past = _list(conn, size=50, page=3)
    assert past.total == 15 and past.items == []


@pytest.mark.parametrize(
    ("filters", "expect"),
    [
        ({"ticker": ["AMD"]}, {("AMD", "open"), ("AMD", "close")}),
        ({"ticker": ["amd"], "kind": ["close"]}, {("AMD", "close")}),
        ({"stage": ["gate_fail"]}, {("AAPL", "open"), ("DIA", "open")}),
        (
            {"stage": ["open", "closed"]},
            {("SPY", "open"), ("QQQ", "open"), ("NVDA", "open"), ("AMD", "open")},
        ),
        ({"structure": ["long_call"]}, {("NVDA", "open")}),
        ({"exit_reason": ["reallocate"]}, {("AMD", "open"), ("AMD", "close")}),
        ({"reason_code": ["exit:reallocate"]}, {("AMD", "close")}),
        ({"reason_code": ["risk_assessed", "owner_approve"]}, {("SPY", "open")}),
        ({"min_net_ev": 30.0}, {("IWM", "open"), ("XLE", "open"), ("SPY", "open")}),
        ({"account_profile": ["cash_debit"]}, {("SPY", "open")}),
        ({"q": "chain-fx-spy"}, {("SPY", "open")}),
        ({"q": "run-fx-risk"}, {("SPY", "open")}),
        ({"q": "swap-fx-amd-xle"}, {("AMD", "close"), ("XLE", "open")}),
        (
            {"date": "today"},
            {
                ("GOOGL", "open"),
                ("MSFT", "open"),
                ("QQQ", "close"),
                ("NVDA", "open"),
                ("IWM", "open"),
                ("AMZN", "open"),
                ("TSLA", "open"),
                ("META", "open"),
                ("AAPL", "open"),
            },
        ),
    ],  # fmt: skip
)
def test_filters(conn: sqlite3.Connection, filters: dict, expect: set) -> None:
    got = {(i.ticker, i.kind) for i in _list(conn, **filters).items}
    assert got == expect


def test_filter_min_pop_and_hash_prefix(conn: sqlite3.Connection) -> None:
    tsla = {i.ticker for i in _list(conn, min_pop=0.45).items}
    assert "TSLA" not in tsla and "GOOGL" in tsla  # TSLA's managed PoP is 0.40
    h = H("pos-amd")
    (row,) = _list(conn, q=h[:10].upper()).items
    assert row.proposal_hash == h


def test_date_presets() -> None:
    today = NOW.date()
    f = TradeFilters
    assert date_range(f(date="today"), today) == (today, today)
    assert date_range(f(date="7d"), today) == (today - dt.timedelta(days=6), today)
    assert date_range(f(date="30d"), today) == (today - dt.timedelta(days=29), today)
    assert date_range(f(date="mtd"), today) == (dt.date(2026, 9, 1), today)
    assert date_range(f(date="ytd"), today) == (dt.date(2026, 1, 1), today)
    assert date_range(f(date="all"), today) == (None, None)
    custom = f(date="custom", date_from=dt.date(2026, 9, 1), date_to=dt.date(2026, 9, 25))
    assert date_range(custom, today) == (dt.date(2026, 9, 1), dt.date(2026, 9, 25))


def test_date_filters_on_the_fixture(conn: sqlite3.Connection) -> None:
    week = {i.ticker for i in _list(conn, date="7d").items}
    assert "DIA" in week and "AMD" in week  # 4 and 1 days old; AMD's open (8 d) is out
    assert ("AMD", "open") not in {(i.ticker, i.kind) for i in _list(conn, date="7d").items}
    custom = _list(conn, date="custom", date_from=NOW.date() - dt.timedelta(days=8),
                   date_to=NOW.date() - dt.timedelta(days=8))  # fmt: skip
    assert {(i.ticker, i.kind) for i in custom.items} == {("AMD", "open")}


@pytest.mark.parametrize("sort", ["time", "ticker", "contracts", "limit", "net_ev", "pop",
                                  "slippage_bps", "realized_pnl"])  # fmt: skip
def test_sorts_put_nulls_last(conn: sqlite3.Connection, sort: str) -> None:
    for direction in ("asc", "desc"):
        r = _list(conn, sort=sort, direction=direction)
        assert r.total == 15 and len({i.proposal_hash for i in r.items}) == 15
    by_ev = [i.net_ev for i in _list(conn, sort="net_ev").items]
    vals = [v for v in by_ev if v is not None]
    assert vals == sorted(vals, reverse=True) and by_ev[-1] is None
    by_ticker = [i.ticker for i in _list(conn, sort="ticker", direction="asc").items]
    assert by_ticker == sorted(by_ticker)  # type: ignore[type-var]


def test_filter_options(conn: sqlite3.Connection) -> None:
    o = load_filter_options(conn, now=NOW)
    assert "SPY" in o.tickers and "XLE" in o.tickers and o.kinds == ["close", "open"]
    assert o.structures == ["long_call", "vertical_debit"]
    assert o.exit_reasons == ["profit_target", "reallocate"]
    assert {"code": "exit:reallocate", "label": "Exit: close to reallocate"} in o.reason_codes
    assert o.account_profiles == ["cash_debit"]
    assert "gate_fail" in o.stages and "custom" in o.date_presets


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_resolves_every_kind(conn: sqlite3.Connection) -> None:
    def first(q: str):  # noqa: ANN202
        return search(conn, q, now=NOW).matches[0]

    assert first("spy").route == "/trades?ticker=SPY"
    assert first("run-fx-risk").route == f"/trades/{H('pos-spy')}"
    assert first("chain-fx-spy").route == f"/trades/{H('pos-spy')}"
    sid = conn.execute(
        "SELECT id FROM open_structures WHERE open_proposal_hash = ?", (H("pos-amd"),)
    ).fetchone()[0]
    m = first(sid)
    assert m.kind == "structure" and m.route == f"/trades/{H('pos-amd')}"
    m = first(H("p-aapl")[:12])
    assert m.kind == "trade" and m.route == f"/trades/{H('p-aapl')}"
    assert first("XL").route == "/trades?ticker=XLE"  # ticker prefix
    assert search(conn, "  ", now=NOW).matches == []
    assert search(conn, "nothing-here", now=NOW).matches == []


# ---------------------------------------------------------------------------
# detail: every section
# ---------------------------------------------------------------------------


def test_detail_header_and_payoff(conn: sqlite3.Connection) -> None:
    d = _detail(conn, "pos-spy")
    assert d.header.row.ticker == "SPY" and d.header.lifecycle == "open"
    assert [leg.side for leg in d.header.legs] == ["long", "short"]
    assert d.header.opened_at is not None and d.header.closed_at is None
    p = d.payoff
    assert p.contracts == 3 and p.error is None and len(p.points) >= 3
    assert p.max_loss == pytest.approx(4.15 * 100 * 3) and p.max_gain == pytest.approx(
        (10 - 4.15) * 100 * 3
    )
    assert p.breakevens == [pytest.approx(664.15)]
    assert p.entry_spot == pytest.approx(663.40) and p.latest_spot == pytest.approx(661.30)
    xs = [pt.spot for pt in p.points]
    assert xs == sorted(xs) and min(xs) <= 661.30 <= max(xs)
    assert p.mark_pnl is not None and p.mark_at is not None  # latest monitor legs


def test_detail_quant(conn: sqlite3.Connection) -> None:
    q = _detail(conn, "pos-spy").quant
    assert q.pop == pytest.approx(0.58) and q.contracts == 3 and q.analytics is not None
    assert q.analytics.exit_model is not None and q.analytics.account_profile == "cash_debit"
    assert q.net_ev_managed == pytest.approx(q.analytics.exit_model.managed.net_ev)
    assert q.net_ev_hold == pytest.approx(q.analytics.exit_model.static.net_ev)
    assert q.analytics_error is None
    stub = _detail(conn, "p-googl").quant  # a stub payload: headline only, no crash
    assert stub.net_ev_managed == pytest.approx(12.4) and stub.analytics is None
    assert stub.analytics_error is not None


def test_detail_decision_trail(conn: sqlite3.Connection) -> None:
    t = _detail(conn, "pos-spy").decisions
    assert t.chain_run_id == "chain-fx-spy"
    stages = [i.stage for i in t.items]
    assert stages[0] == "candidate" and stages[-1] == "order"
    assert all(i.subject in {"SPY", "market", "session"} for i in t.items)  # not XLU
    assert [i.at for i in t.items] == sorted(i.at for i in t.items)  # type: ignore[type-var]
    mine = [i for i in t.items if i.this_trade]
    assert {i.reason_code for i in mine} >= {"risk_assessed", "gate:pass", "owner_approve"}
    risk = next(i for i in t.items if i.reason_code == "risk_assessed")
    assert risk.reason_label == "Risk reviewed it" and risk.confidence == pytest.approx(0.7)
    call = t.persona_calls[risk.persona_call_id]  # type: ignore[index]
    assert call.persona == "risk" and call.model == "claude-sonnet-5"
    assert call.input_tokens == 7200 and call.cost_usd == pytest.approx(0.0351)
    assert call.prompt_text and len(call.prompt_sha256) == 64
    assert set(t.persona_calls) == {"pc-fx-research", "pc-fx-quant", "pc-fx-risk"}


def test_detail_gate_two_violations_and_token_hidden(conn: sqlite3.Connection) -> None:
    (g,) = _detail(conn, "p-aapl").gate
    assert not g.passed and [v.code for v in g.violations] == [
        "per_underlying_limit",
        "spread_too_wide",
    ]
    assert [v.label for v in g.violations] == [
        "Gate: over the per-underlying limit",
        "Gate: bid-ask spread too wide",
    ]
    assert g.violations[0].detail.startswith("max loss $5,400")
    assert _detail(conn, "p-aapl").header.lifecycle_failed == "gate"
    spy = _detail(conn, "pos-spy")
    (sg,) = spy.gate
    assert sg.passed and sg.token_version == "arc2" and sg.account_snapshot["equity"] == 100000
    token = conn.execute(
        "SELECT token FROM gate_decisions WHERE proposal_hash = ?", (H("pos-spy"),)
    ).fetchone()[0]
    assert token.startswith("arc2.") and token not in spy.model_dump_json()  # never served


def _fixture_tokens(conn: sqlite3.Connection) -> list[str]:
    """Every gate token (and its signature, the secret-bearing part) in the fixture."""
    toks = [r[0] for r in conn.execute("SELECT token FROM gate_decisions WHERE token IS NOT NULL")]
    return toks + [t.rsplit(".", 1)[-1] for t in toks]


def test_fixture_writes_real_minted_tokens(conn: sqlite3.Connection) -> None:
    from arc.gate.token import BandToken, client_order_id, verify_band

    rows = conn.execute(
        """SELECT g.proposal_hash, g.token, o.client_order_id FROM gate_decisions g
           JOIN orders o ON o.proposal_hash = g.proposal_hash WHERE g.token IS NOT NULL"""
    ).fetchall()
    assert len(rows) >= 5
    for h, token, coid in rows:
        exp = dt.datetime.fromtimestamp(BandToken.parse(token).expires_epoch - 1, tz=ET)
        # a real, signature-valid arc2 token for this proposal, not a placeholder
        parsed = verify_band(token, secret=fixture.FIXTURE_GATE_SECRET, now=exp, proposal_hash=h)
        assert parsed.lo_cents == 100 and parsed.hi_cents == 300 and parsed.max_steps == 3
        assert coid == client_order_id(token, 1)


def test_detail_never_serves_token(conn: sqlite3.Connection, client: TestClient) -> None:
    secrets = _fixture_tokens(conn)
    assert secrets
    hashes = [r[0] for r in conn.execute("SELECT proposal_hash FROM proposals")]
    served: list[str] = []
    for h in hashes:
        d = load_trade(conn, h, now=NOW)
        assert d is not None
        served.append(d.model_dump_json())
        served.append(client.get(f"/api/trades/{h}").text)
        served.append(search(conn, h, now=NOW).model_dump_json())
        served.append(search(conn, h[:12], now=NOW).model_dump_json())
    served.append(_list(conn, size=200).model_dump_json())
    served.append(client.get("/api/trades", params={"size": 200}).text)
    for q in ("SPY", "AMD", "arc2", "s1"):
        served.append(search(conn, q, now=NOW).model_dump_json())
        served.append(client.get("/api/search", params={"q": q}).text)
    blob = "\n".join(served)
    for secret in secrets:
        assert secret not in blob
    assert "client_order_id" not in blob
    (o,) = _detail(conn, "pos-spy").execution.orders  # type: ignore[union-attr]
    assert re.fullmatch(r"arc2\.s1·[0-9a-f]{12}", o.client_order_ref)


def test_openapi_has_no_client_order_id() -> None:
    from arc.tower.openapi import spec

    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            out = set(node)
            for v in node.values():
                out |= keys(v)
            return out
        if isinstance(node, list):
            return set().union(*(keys(v) for v in node)) if node else set()
        return set()

    s = spec()
    props = keys(s)
    assert "client_order_id" not in props
    assert "client_order_ref" in s["components"]["schemas"]["OrderView"]["properties"]


@pytest.mark.parametrize(
    ("coid", "expect"),
    [
        ("arc2.AAAA.BBBB.s3", r"arc2\.s3·[0-9a-f]{12}"),
        ("arc1.AAAA.BBBB", r"arc1·[0-9a-f]{12}"),
        ("arc-0123abcd", r"ext·[0-9a-f]{12}"),
        ("manual.s2", r"ext\.s2·[0-9a-f]{12}"),
        ("", r"none"),
        (None, r"none"),
    ],
)
def test_client_order_ref_shapes(coid: str | None, expect: str) -> None:
    ref = client_order_ref(coid)
    assert re.fullmatch(expect, ref)
    if coid:
        assert coid not in ref and "AAAA" not in ref and "BBBB" not in ref
    assert client_order_ref(coid) == ref  # deterministic: matches a broker export


def test_detail_approval(conn: sqlite3.Connection) -> None:
    a = _detail(conn, "pos-spy").approval
    assert a is not None and a.status == "approved" and a.decided_by == "U0OWNER001"
    assert a.ttl_s == 30 * 60 - 30 and a.limit_price == D("4.20")
    assert a.channel == "C0FIXTURE" and a.thread_ts == "1790000000.000100"
    exp = _detail(conn, "p-amzn").approval
    assert exp is not None and exp.status == "expired" and exp.decided_by == "arc:ttl"
    assert _detail(conn, "p-googl").approval is None


def test_detail_execution(conn: sqlite3.Connection) -> None:
    x = _detail(conn, "pos-spy").execution
    assert x is not None and x.status == "filled" and x.token_version == "arc2"
    assert (x.band_lo, x.band_hi, x.max_steps, x.steps_used) == (D("1.00"), D("3.00"), 3, 1)
    assert x.fill_price == D("4.15") and x.limit == D("4.20") and x.mid == D("4.15")
    assert x.slippage_vs_limit == D("-0.05") and x.slippage_vs_mid == D("0")
    (o,) = x.orders
    assert [e.to_state for e in o.events] == ["gated", "approved", "submitted", "filled"]
    assert [(f.qty, f.price) for f in x.fills] == [(3, D("4.15"))]
    working = _detail(conn, "p-meta")
    assert working.execution is not None and working.execution.status == "working"
    assert working.header.lifecycle == "execution"
    assert _detail(conn, "p-iwm").header.lifecycle_failed == "execution"
    assert _detail(conn, "p-googl").execution is None


def test_detail_close_to_reallocate_pair(conn: sqlite3.Connection) -> None:
    amd = _detail(conn, "pos-amd")
    pos = amd.position
    assert pos is not None and pos.status == "closed" and pos.exit_reason == "reallocate"
    assert pos.realized_pnl == D("300.00") and pos.days_held == 7
    assert pos.close_net == D("-5.10") and pos.open_proposal_hash == H("pos-amd")
    (ex,) = pos.exits
    assert ex.proposal_hash == H("exit-amd") and ex.stage == "filled"
    (sw,) = pos.swaps
    assert (sw.close_ticker, sw.open_ticker) == ("AMD", "XLE")
    assert sw.open_proposal_hash == H("p-xle-swap") and sw.suggestion["edge"] == 18.2
    assert amd.header.lifecycle == "closed" and amd.header.closed_at is not None
    close = _detail(conn, "exit-amd")
    assert close.header.row.kind == "close" and close.header.lifecycle == "filled"
    assert close.position is not None and close.position.structure_id == pos.structure_id
    assert [i.reason_code for i in close.decisions.items][:2] == [
        "realloc:risk_approved",
        "exit:reallocate",
    ]
    xle = _detail(conn, "p-xle-swap")
    assert xle.position is not None and xle.position.structure_id is None
    assert [w.id for w in xle.position.swaps] == ["swap-fx-amd-xle"]
    assert xle.header.lifecycle_failed == "approval"  # expired


def test_detail_pending_exit(conn: sqlite3.Connection) -> None:
    qqq = _detail(conn, "pos-qqq")
    assert qqq.header.lifecycle == "exit" and qqq.position is not None
    assert qqq.position.exit_pending and qqq.position.exit_reason == "profit_target"
    assert [e.proposal_hash for e in qqq.position.exits] == [H("exit-qqq")]


# -- realized P&L is booked per close tranche (review round 1) -------------------------


def _tranche_db(fx_db: Path, tmp_path: Path, closes: list[tuple[int, str]]) -> Path:
    """Copy the fixture; SPY (entry 4.15) opened 4, closed in *closes* (qty, fill) tranches.

    Each close is an ``executions`` row + ``OpenStructureRepo.reduce`` like the ladder,
    and AMD's ``outcomes`` row is dropped so every figure comes from the structures.
    """
    from arc.store.execution import OpenStructureRepo

    db = tmp_path / "tranches.db"
    src = sqlite3.connect(fx_db)
    c = sqlite3.connect(db)  # foreign keys off: the close proposals are not needed here
    src.backup(c)
    src.close()
    c.row_factory = sqlite3.Row
    c.execute("DROP TRIGGER outcomes_no_delete")  # test copy only: outcomes is append-only
    c.execute("DELETE FROM outcomes")
    sid = c.execute(
        "SELECT id FROM open_structures WHERE open_proposal_hash = ?", (H("pos-spy"),)
    ).fetchone()[0]
    c.execute("UPDATE open_structures SET contracts = 4 WHERE id = ?", (sid,))
    c.execute(
        "UPDATE executions SET contracts = 4, filled_qty = 4 WHERE proposal_hash = ?",
        (H("pos-spy"),),
    )
    repo = OpenStructureRepo(c)
    for i, (qty, fill) in enumerate(closes):
        c.execute(
            """INSERT INTO executions (proposal_hash, kind, structure_id, status, token_version,
                 band_lo, band_hi, max_steps, contracts, filled_qty, fill_price, started_at)
               VALUES (?, 'close', ?, 'filled', 'arc2', '-6', '-4', 3, ?, ?, ?, ?)""",
            (f"close-tranche-{i}", sid, qty, qty, fill, NOW.isoformat()),
        )
        repo.reduce(sid, closed_qty=qty, close_net=D(fill), now=NOW, commit=False)
    c.commit()
    c.close()
    return db


def test_realized_pnl_sums_every_close_tranche(fx_db: Path, tmp_path: Path) -> None:
    # ladder truth: 2 x 100 x 1.00 + 2 x 100 x 0.50 = +300 (not 100 x remaining contracts)
    db = _tranche_db(fx_db, tmp_path, [(2, "-5.15"), (2, "-4.65")])
    c = connect_ro(db)
    try:
        r = load_trades(c, TradeFilters(ticker=["SPY"]), now=NOW)
        (spy,) = [i for i in r.items if i.proposal_hash == H("pos-spy")]
        assert spy.stage == "closed" and spy.realized_pnl == pytest.approx(300.0)
        assert r.summary.realized_pnl == pytest.approx(300.0) and r.summary.realized_count == 1
        d = load_trade(c, H("pos-spy"), now=NOW)
        assert d is not None and d.position is not None
        assert d.position.realized_pnl == D("300.00")
        # AMD (one close tranche, outcomes dropped) falls back to the same rule
        amd = load_trade(c, H("pos-amd"), now=NOW)
        assert amd is not None and amd.position is not None
        assert amd.position.realized_pnl == D("300.00")
        assert amd.header.row.realized_pnl == pytest.approx(300.0)
    finally:
        c.close()


def test_realized_pnl_partial_close_still_open(fx_db: Path, tmp_path: Path) -> None:
    db = _tranche_db(fx_db, tmp_path, [(1, "-5.15")])  # 1 of 4 closed at +1.00
    c = connect_ro(db)
    try:
        (spy,) = [
            i for i in load_trades(c, TradeFilters(ticker=["SPY"]), now=NOW).items
            if i.proposal_hash == H("pos-spy")
        ]  # fmt: skip
        assert spy.stage == "open" and spy.realized_pnl == pytest.approx(100.0)
        d = load_trade(c, H("pos-spy"), now=NOW)
        assert d is not None and d.position is not None
        assert d.position.status == "open" and d.position.contracts == 3
        assert d.position.realized_pnl == D("100.00")
    finally:
        c.close()


def test_realized_pnl_partial_close_then_expiry_settlement(fx_db: Path, tmp_path: Path) -> None:
    """Reconcile settles the rest at expiry without an execution: its contracts count too."""
    from arc.store.execution import OpenStructureRepo

    db = _tranche_db(fx_db, tmp_path, [(1, "-5.15")])
    w = sqlite3.connect(db)
    w.row_factory = sqlite3.Row
    sid = w.execute(
        "SELECT id FROM open_structures WHERE open_proposal_hash = ?", (H("pos-spy"),)
    ).fetchone()[0]
    OpenStructureRepo(w).reduce(sid, closed_qty=3, close_net=D("-4.15"), now=NOW)  # flat
    w.close()
    c = connect_ro(db)
    try:
        (spy,) = [
            i for i in load_trades(c, TradeFilters(ticker=["SPY"]), now=NOW).items
            if i.proposal_hash == H("pos-spy")
        ]  # fmt: skip
        assert spy.stage == "closed" and spy.realized_pnl == pytest.approx(100.0)
        d = load_trade(c, H("pos-spy"), now=NOW)
        assert d is not None and d.position is not None
        assert d.position.realized_pnl == D("100.00")
    finally:
        c.close()


def test_realized_pnl_none_before_any_close(conn: sqlite3.Connection) -> None:
    (spy,) = [i for i in _list(conn, ticker=["SPY"]).items if i.proposal_hash == H("pos-spy")]
    assert spy.stage == "open" and spy.realized_pnl is None
    pos = _detail(conn, "pos-spy").position
    assert pos is not None and pos.realized_pnl is None


def test_detail_outcome_and_review(conn: sqlite3.Connection) -> None:
    o = _detail(conn, "pos-amd").outcome
    assert o.outcome is not None and o.outcome.realised_pnl == D("300.00")
    assert o.outcome.hold_to_expiry_shadow_pnl == D("412.00")
    assert o.outcome.max_adverse_excursion == D("-84.00") and o.outcome.pnl_vs_ev == D("275.20")
    (rv,) = o.reviews
    assert rv.label == "good_decision_good_outcome" and rv.root_cause == "exit_management"
    assert len(rv.cites) == 1 and rv.cites[0].startswith("dec-fx-")


def test_detail_market_context(conn: sqlite3.Connection) -> None:
    m = _detail(conn, "pos-spy").market
    assert m.spot == pytest.approx(663.40) and m.regime_label == "bull" and len(m.legs) == 2
    assert all(q.bid is not None and q.ask is not None for q in m.legs)
    assert m.quotes_as_of is not None
    assert m.regime is not None and m.regime.current == "bull"
    assert m.regime.snapshot_id == "snap-fx-spy" and m.regime.stickiness == pytest.approx(0.82)
    assert m.regime.z is None and m.regime.vol_state is None  # a v1 entry
    assert m.candidate is not None and m.candidate.corroboration == 3
    assert any(s.startswith("https://www.sec.gov/") for s in m.candidate.sources)


def test_detail_market_context_regime_v2(tmp_path: Path) -> None:
    """E17.1 (D77): a v2 regime entry shows its z, run length and vol state."""
    db = fixture.build(tmp_path / "arc.db", NOW)
    rw = sqlite3.connect(db)
    # a throwaway copy of the fixture: lift the append-only guard to rewrite one payload
    rw.execute("DROP TRIGGER context_entries_status_only")
    (raw,) = rw.execute(
        "SELECT payload FROM context_entries WHERE id = 'ctx-fx-regime-spy'"
    ).fetchone()
    payload = json.loads(raw)
    payload["regime"].update(
        model="v2", z=1.37, run_length=4, margin_z=0.37, vol_state="low", rv20_pct_rank=18.0
    )
    rw.execute(
        "UPDATE context_entries SET payload = ? WHERE id = 'ctx-fx-regime-spy'",
        (json.dumps(payload),),
    )
    rw.commit()
    rw.close()
    c = connect_ro(db)
    try:
        r = _detail(c, "pos-spy").market.regime
    finally:
        c.close()
    assert r is not None and r.model == "v2" and r.z == pytest.approx(1.37)
    assert r.run_length == 4 and r.vol_state == "low" and r.rv20_pct_rank == pytest.approx(18.0)
    assert r.margin_z == pytest.approx(0.37)


def test_detail_manifest(conn: sqlite3.Connection) -> None:
    mf = _detail(conn, "pos-spy").manifest
    assert mf is not None and mf.run_id == "run-fx-risk" and mf.route == "/ops/runs/run-fx-risk"
    assert mf.git_sha == fixture._trades_module().GIT_SHA and mf.config_version == "7"
    assert set(mf.config_hashes) == {"routines.yaml", "exits.yaml", "costs.yaml"}
    assert mf.models_served == ["claude-sonnet-5"]
    assert _detail(conn, "p-googl").manifest is None


def test_detail_context_reads(conn: sqlite3.Connection) -> None:
    """E8.8f Context tab: the entries of every snapshot this trade's own steps read, per kind."""
    d = _detail(conn, "pos-spy")
    c = d.context
    assert "snap-fx-spy" in c.snapshot_ids
    assert c.total == sum(k.count for k in c.kinds) >= 1
    regime = next(k for k in c.kinds if k.kind == "regime")
    assert [e.id for e in regime.entries] == ["ctx-fx-regime-spy"]
    assert regime.entries[0].subject == "SPY" and regime.entries[0].produced_by == "features"
    # Only this trade's steps count: chain context rows (other tickers) add no snapshot.
    own = {i.inputs_snapshot_id for i in d.decisions.items if i.this_trade and i.inputs_snapshot_id}
    assert own <= set(c.snapshot_ids)
    empty = _detail(conn, "p-googl").context
    assert empty.total == 0 and empty.kinds == [] and empty.snapshot_ids == []


def test_detail_manifest_reads(conn: sqlite3.Connection) -> None:
    """E8.8f Audit tab: declared reads + per-kind input counts from the run manifest."""
    mf = _detail(conn, "pos-spy").manifest
    assert mf is not None
    assert isinstance(mf.snapshot_ids, list) and isinstance(mf.input_counts, dict)
    assert all(isinstance(v, int) for v in mf.input_counts.values())


def test_detail_risk_narrative_is_stored_verbatim(conn: sqlite3.Connection) -> None:
    """The Why tab parses Risk's narrative client-side only; the API serves it untouched."""
    h = _detail(conn, "pos-spy").header
    assert h.risk_narrative == fixture._trades_module().SPY_RISK_NARRATIVE


def test_detail_minimal_trade_renders_with_empty_sections(conn: sqlite3.Connection) -> None:
    d = _detail(conn, "p-googl")
    assert d.gate == [] and d.approval is None and d.execution is None and d.position is None
    assert d.outcome.outcome is None and d.outcome.reviews == []
    assert d.decisions.items == [] and d.market.regime is None
    assert load_trade(conn, "f" * 64, now=NOW) is None


# ---------------------------------------------------------------------------
# optional tables missing: never an error
# ---------------------------------------------------------------------------


def test_missing_optional_tables(fx_db: Path, tmp_path: Path) -> None:
    db = tmp_path / "stripped.db"
    src = sqlite3.connect(fx_db)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    for t in ("outcomes", "decision_reviews", "decision_review_citations", "decisions",
              "persona_calls", "market_contexts", "run_manifests", "swaps", "order_events",
              "fills", "context_snapshots", "routine_runs"):  # fmt: skip
        dst.execute(f"DROP TABLE IF EXISTS {t}")
    dst.commit()
    dst.close()
    c = connect_ro(db)
    try:
        r = load_trades(c, TradeFilters(reason_code=["x"]), now=NOW)
        assert r.total == 0
        r = load_trades(c, TradeFilters(min_net_ev=0.0), now=NOW)
        assert r.total == 0  # no market_contexts: no net EV to pass the floor
        r = load_trades(c, TradeFilters(q="chain-fx-spy"), now=NOW)
        assert r.total == 1  # proposals.chain_run_id still matches
        full = load_trades(c, TradeFilters(), now=NOW)
        assert full.total == 15 and all(i.net_ev is None for i in full.items)
        for i in full.items:
            d = load_trade(c, i.proposal_hash, now=NOW)
            assert d is not None and d.outcome.outcome is None and d.decisions.items == []
            assert d.context.total == 0  # no context_snapshots: an empty Context tab
        opts = load_filter_options(c, now=NOW)
        assert opts.reason_codes == [] and opts.account_profiles == []
        assert search(c, "SPY", now=NOW).matches
    finally:
        c.close()
    with TestClient(create_app(db, clock=lambda: NOW)) as cl:
        assert cl.get(f"/api/trades/{H('pos-amd')}").status_code == 200


def test_pre_execution_store_is_empty_not_an_error(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE proposals (proposal_hash TEXT)")
    conn.commit()
    conn.close()
    c = connect_ro(db)
    try:
        assert load_trades(c, TradeFilters(), now=NOW).total == 0
        assert load_filter_options(c, now=NOW).tickers == []
        assert search(c, "SPY", now=NOW).matches == []
    finally:
        c.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_list_with_url_filters(client: TestClient) -> None:
    r = client.get("/api/trades", params={"ticker": "AMD,SPY", "kind": "open", "sort": "ticker",
                                           "dir": "asc", "size": 1})  # fmt: skip
    assert r.status_code == 200
    body = TradeListResponse.model_validate(r.json())
    assert body.total == 2 and [i.ticker for i in body.items] == ["AMD"]
    assert body.filters.ticker == ["AMD", "SPY"] and body.as_of == NOW
    r = client.get("/api/trades?stage=gate_fail&stage=expired")
    assert {i["ticker"] for i in r.json()["items"]} == {"AAPL", "DIA", "AMZN", "XLE"}


@pytest.mark.parametrize(
    "query",
    ["stage=nope", "date=decade", "min_pop=2", "size=500", "page=0", "sort=bogus", "kind=both"],
)
def test_api_list_rejects_bad_params(client: TestClient, query: str) -> None:
    r = client.get(f"/api/trades?{query}")
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_api_detail_filters_search(client: TestClient) -> None:
    r = client.get(f"/api/trades/{H('pos-spy')}")
    assert r.status_code == 200
    d = TradeDetail.model_validate(r.json())
    assert d.header.row.ticker == "SPY" and d.manifest is not None
    miss = client.get(f"/api/trades/{'0' * 64}")
    assert miss.status_code == 404 and miss.json()["error"] == "not_found"
    opts = TradeFilterOptions.model_validate(client.get("/api/trades/filters").json())
    assert "AMD" in opts.tickers
    s = SearchResponse.model_validate(client.get("/api/search", params={"q": "AMD"}).json())
    assert s.matches[0].route == "/trades?ticker=AMD"


def test_api_is_get_only(client: TestClient) -> None:
    for path in ("/api/trades", f"/api/trades/{H('pos-spy')}", "/api/search"):
        assert client.post(path).status_code == 405
        assert client.delete(path).status_code == 405


def test_api_never_writes_the_store(fx_db: Path) -> None:
    before = hashlib.sha256(fx_db.read_bytes()).hexdigest()
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as c:
        c.get("/api/trades")
        c.get("/api/trades/filters")
        c.get(f"/api/trades/{H('pos-amd')}")
        c.get("/api/search?q=SPY")
    assert hashlib.sha256(fx_db.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# migration 017 + performance on a 100k-proposal store
# ---------------------------------------------------------------------------


def test_migration_017_indexes(tmp_path: Path) -> None:
    c = connect(tmp_path / "m.db")
    migrate(c)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_gate_decisions_proposal", "idx_market_contexts_trade", "idx_proposals_day",
            "idx_orders_proposal", "idx_open_structures_exit"} <= names  # fmt: skip
    c.close()


@pytest.fixture(scope="module")
def big_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("big") / "arc.db"
    synthetic.build_synthetic(db, 100_000)
    return db


def test_list_query_plan_uses_indexes(big_db: Path) -> None:
    c = connect_ro(big_db)
    try:
        from arc.tower.data_trades import _base

        plan = " | ".join(
            str(r[3]) for r in c.execute("EXPLAIN QUERY PLAN " + _base(c) + " SELECT * FROM trades")
        )
    finally:
        c.close()
    assert "idx_gate_decisions_proposal" in plan and "idx_market_contexts_trade" in plan
    assert "SCAN gate_decisions" not in plan and "SCAN market_contexts" not in plan
    assert "SCAN open_structures" not in plan and "SCAN outcomes" not in plan


@pytest.mark.serial  # wall-clock budget: runs alone, after the parallel pass (Makefile)
@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"ticker": ["SPY", "QQQ"], "stage": ["closed", "filled"]},
        {"min_net_ev": 10.0, "min_pop": 0.5},
        {"account_profile": ["cash_debit"], "kind": ["open"]},
        {"date": "ytd", "structure": ["vertical_debit"], "exit_reason": ["take_profit"]},
        {"q": "run100"},
    ],
)
def test_list_under_1s_on_100k_proposals(big_db: Path, filters: dict) -> None:
    # 1 s budget: on shared CI runners the slowest filter/sort lands near 0.5 s (flaked at 504 ms).
    c = connect_ro(big_db)
    try:
        for sort in ("time", "net_ev"):
            best = min(
                _timed(lambda s=sort: load_trades(c, TradeFilters(**filters), now=NOW, sort=s))
                for _ in range(2)
            )
            assert best < 1.0, f"{filters} sort={sort}: {best * 1000:.0f} ms"
    finally:
        c.close()


def _timed(fn) -> float:  # noqa: ANN001
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def test_detail_floor_exit_facts(fx_db: Path, tmp_path: Path) -> None:
    """E6.4a: a remaining-EV floor close shows its facts on the trade detail."""
    import json
    import shutil

    from arc.journal.store import JournalStore

    db = tmp_path / "floor.db"
    shutil.copy(fx_db, db)
    c = connect(db)
    sid = c.execute(
        "SELECT id FROM open_structures WHERE open_proposal_hash = ?", (H("pos-amd"),)
    ).fetchone()[0]
    c.execute("UPDATE open_structures SET exit_reason = 'remaining_ev_floor' WHERE id = ?", (sid,))
    review = {
        "remaining_ev": -51.05, "remaining_ev_per_bp": -0.111, "buying_power": 459.5,
        "ev_floor": -0.01, "ev_floor_window": "eod", "end_of_day": True,
        "entry_managed_net_ev": 69.07, "minutes_since_fill": 42.0, "signals": [],
    }  # fmt: skip
    JournalStore(c).record(
        persona="quant", stage="exit", subject="AMD", choice="selected",
        reason_code=ReasonCode.EXIT_EV_FLOOR, proposal_hash=H("exit-amd"),
        payload={"structure_id": sid, "review": review}, at=NOW,
    )  # fmt: skip
    c.commit()
    c.close()
    ro = connect_ro(db)
    try:
        d = load_trade(ro, H("pos-amd"), now=NOW)
        assert d is not None and d.position is not None
        f = d.position.floor_exit
        assert f is not None and f.window == "end_of_day" and f.floor == -0.01
        assert f.remaining_ev_per_bp == -0.111 and f.minutes_since_fill == 42.0
        assert f.entry_managed_net_ev_per_bp == pytest.approx(69.07 / 459.5, abs=1e-6)
        assert json.loads(d.model_dump_json())["position"]["floor_exit"]["window"] == "end_of_day"
        other = load_trade(ro, H("pos-qqq"), now=NOW)
        assert other is not None and other.position is not None
        assert other.position.floor_exit is None
    finally:
        ro.close()
