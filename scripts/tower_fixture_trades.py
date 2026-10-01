"""Trades drill-down rows for the tower fixture DB (E8.7b).

Called by :func:`scripts.tower_fixture_db.build` after the E8.7a rows exist. Adds, for
the SPY open (the "full" trade), every section the Trades detail shows:

- a Scout candidate with sources, a chain (``routine_runs``) with a D27 run manifest,
  three persona calls (Director / Quant / Risk) and the decision trail through gate,
  approval and order fill, plus chain-level Director market reads;
- a full ``MarketContext`` (leg quotes, analytics priced by the real exit model and cost
  model) and a ``regime`` context entry in the decisions' input snapshot;
- order state-machine events;

and a close-to-reallocate pair on AMD: AMD's close (``kind='close'``, a swap) filled
and closed the structure, and the swap's replacement XLE open was proposed and passed
the gate, then expired. AMD also gets an ``outcomes`` row and an owner review that
cites one of its decisions. Every new proposal is older than 24 h so the Overview's
"Today's proposals" and positions counts stay as E8.7a pinned them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from arc.context.ttl import to_db
from arc.store.repos import OrderRepo

if TYPE_CHECKING:
    import sqlite3

    from arc.models import Structure

__all__ = ["add_trade_rows"]

GIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def _ins(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ",".join(row)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",  # noqa: S608
        list(row.values()),
    )


def _decision(
    conn: sqlite3.Connection,
    n: int,
    *,
    at: dt.datetime,
    persona: str,
    stage: str,
    subject: str,
    choice: str,
    code: str,
    text: str = "",
    h: str | None = None,
    chain: str | None = None,
    run: str | None = None,
    snap: str | None = None,
    call: str | None = None,
    confidence: float | None = None,
) -> str:
    from arc.journal.reasons import ReasonCode

    ReasonCode(code)  # a fixture typo must fail the build, not render as unknown
    did = f"dec-fx-{n:03d}"
    _ins(conn, "decisions", {
        "id": did, "chain_run_id": chain, "run_id": run, "persona": persona, "stage": stage,
        "subject": subject, "choice": choice, "reason_code": code, "reason_text": text,
        "confidence": confidence, "inputs_snapshot_id": snap, "persona_call_id": call,
        "proposal_hash": h, "payload": "{}", "at": to_db(at),
    })  # fmt: skip
    return did


def _full_market_context(h: str, st: Structure, *, spot: float, at: dt.datetime) -> str:
    """A real MarketContext payload: quotes per leg + analytics from the exit/cost models."""
    from arc.backtest.costs import load_cost_model
    from arc.data.base import OptionContract, OptionGreeks
    from arc.exits import load_exit_config, model_exits
    from arc.journal.models import LegQuote, MarketContext
    from arc.pipeline.analytics import build_analytics
    from arc.pipeline.market import PricedStructure
    from arc.structures import parse_occ

    contracts: dict[str, OptionContract] = {}
    quotes: list[LegQuote] = []
    for i, leg in enumerate(st.legs):
        occ = parse_occ(leg.occ_symbol)
        mid = float(leg.premium or 0)
        bid, ask = round(mid - 0.05, 2), round(mid + 0.05, 2)
        iv = 0.16 + 0.01 * i
        contracts[occ.format()] = OptionContract(
            symbol=leg.occ_symbol, underlying=occ.root, expiration=occ.expiration,
            strike=float(occ.strike), option_type="call" if occ.kind.name == "CALL" else "put",
            bid=bid, ask=ask, mid=mid, bid_size=20, ask_size=35, open_interest=8000,
            volume=1500, implied_volatility=iv,
            greeks=OptionGreeks(delta=0.55 if leg.side.value == "long" else 0.35),
        )  # fmt: skip
        quotes.append(
            LegQuote(occ_symbol=leg.occ_symbol, bid=bid, ask=ask, mid=mid, iv=iv, quote_time=at)
        )
    priced = PricedStructure(st, contracts, spot=spot, atm_iv=0.16, spot_as_of=at)
    cost = load_cost_model()
    em = model_exits(
        st, load_exit_config().policy_for(st.kind), spot=spot, iv=0.16, r=0.04,
        spreads=priced.leg_spreads(), cost=cost,
    )  # fmt: skip
    analytics = build_analytics(
        priced, cost=cost, regime={"last_close": spot - 2.1, "vol": {"hv20": 0.13}},
        exit_model=em, account_profile="cash_debit",
    )  # fmt: skip
    mc = MarketContext(
        proposal_hash=h, subject=st.legs[0].occ_symbol[:3], underlying_last=spot, atm_iv=0.16,
        ivr=0.34, hv20=0.13, regime="bull", legs=quotes, quotes_as_of=at, at=at,
        analytics=analytics,
    )  # fmt: skip
    return mc.model_dump_json()


def _closing(st: Structure, *, close_net: Decimal) -> Structure:
    """The structure that closes *st*: every leg inverted, priced to *close_net* per share."""
    from arc.models import Leg, LegIntent
    from arc.structures import analyze, parse_occ

    legs = [
        Leg(occ_symbol=leg.occ_symbol,
            side=LegIntent.SHORT if leg.side == LegIntent.LONG else LegIntent.LONG,
            ratio=leg.ratio, premium=leg.premium)
        for leg in st.legs
    ]  # fmt: skip
    # the close is struck at the fill: scale premiums so the net matches it
    expiry = parse_occ(st.legs[0].occ_symbol).expiration
    as_of = expiry - dt.timedelta(days=st.dte)
    k = close_net / analyze(legs, as_of=as_of).net_debit_credit
    legs = [leg.model_copy(update={"premium": (leg.premium or Decimal(0)) * k}) for leg in legs]
    return analyze(legs, as_of=as_of).model_copy(update={"kind": st.kind})


def _order_events(
    conn: sqlite3.Connection, h: str, at: dt.datetime, end: str, run: str | None = None
) -> None:
    """Walk the fixture order for *h* through the state machine to *end*."""
    from arc.models import OrderState

    row = conn.execute("SELECT id FROM orders WHERE proposal_hash = ?", (h,)).fetchone()
    if row is None:
        return
    path = ["gated", "approved", "submitted", end]
    repo = OrderRepo(conn)
    for i, state in enumerate(path):
        repo.transition(
            order_id=row[0], to_state=OrderState(state),
            actor="arc:execution" if i >= 2 else "arc:pipeline",
            detail=f"limit step 1 at {at:%H:%M}" if state == "submitted" else "",
            event_at=to_db(at + dt.timedelta(minutes=3, seconds=10 * i)), run_id=run,
        )  # fmt: skip


def add_trade_rows(  # noqa: PLR0915 - one linear fixture script
    conn: sqlite3.Connection,
    *,
    now: dt.datetime,
    spy_hash: str,
    spy_structure: Structure,
    spy_opened: dt.datetime,
    amd_hash: str,
    amd_structure_id: str,
    amd_opened: dt.datetime,
    filled_hashes: dict[str, dt.datetime],
    make_proposal: Any,
    make_gate: Any,
    make_approval: Any,
    make_execute: Any,
    vert: Any,
) -> dict[str, str]:
    """Add the drill-down rows; returns the new proposal hashes by tag."""
    # -- order events for every filled open ------------------------------------------
    for h, at in filled_hashes.items():
        _order_events(conn, h, at, "filled")

    # -- SPY: the full trade -------------------------------------------------------------
    at = spy_opened
    chain = "chain-fx-spy"
    runs = {
        "scout": "run-fx-scout",
        "director": "run-fx-director",
        "quant": "run-fx-quant",
        "risk": "run-fx-risk",
    }
    for i, (job, rid) in enumerate(runs.items()):
        started = at - dt.timedelta(minutes=12 - 3 * i)
        _ins(conn, "routine_runs", {
            "run_id": rid, "job": job, "chain_run_id": chain, "step_index": i,
            "reason": "schedule" if i == 0 else f"chain:{runs['scout']}",
            "scheduled_for": to_db(started), "started_at": to_db(started),
            "finished_at": to_db(started + dt.timedelta(minutes=2)), "status": "ok",
            "attempts": 1, "inputs_snapshot": json.dumps(["snap-fx-spy"]), "outputs": "[]",
            "summary": f"{job} ok",
        })  # fmt: skip
    conn.execute(
        "UPDATE proposals SET run_id = ?, chain_run_id = ?, spot = ?, regime = ? "
        "WHERE proposal_hash = ?",
        (runs["risk"], chain, "663.40", "bull", spy_hash),
    )
    # candidate with sources + corroboration
    cid = conn.execute(
        "SELECT candidate_id FROM proposals WHERE proposal_hash = ?", (spy_hash,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE candidates SET sources = ?, corroboration = 3, run_id = ?, catalyst_date = ? "
        "WHERE id = ?",
        (
            json.dumps(["https://www.sec.gov/Archives/edgar/data/fixture-8k.htm",
                        "wsj:markets/fixture-story", "youtube:fixture-outlook"]),
            runs["scout"], (at.date() + dt.timedelta(days=9)).isoformat(), cid,
        ),
    )  # fmt: skip
    # regime context entry + the input snapshot the personas read
    prev_day = (at.date() - dt.timedelta(days=1)).isoformat()
    regime_payload = {
        "schema_version": 1, "ticker": "SPY", "as_of": prev_day,
        "last_close": 661.30,
        "regime": {
            "as_of": prev_day, "current": "bull",
            "trailing_return": 0.034, "lookback_days": 20, "step": 5, "n_transitions": 240,
            "transition_matrix": {}, "stickiness": 0.82, "stickiness_by_state": {},
            "expected_duration": 5.6,
        },
        "vol": {"as_of": prev_day, "hv20": 0.13,
                "iv": 0.16, "iv_rank": 0.34, "iv_percentile": 0.41},
        "warnings": [],
    }  # fmt: skip
    _ins(conn, "context_entries", {
        "id": "ctx-fx-regime-spy", "kind": "regime", "subject": "SPY",
        "payload": json.dumps(regime_payload), "schema_version": 1, "produced_by": "features",
        "run_id": None, "chain_run_id": None, "created_at": to_db(at - dt.timedelta(hours=14)),
        "valid_from": to_db(at - dt.timedelta(hours=14)), "expires_at": None,
        "status": "active",
    })  # fmt: skip
    _ins(conn, "context_snapshots", {
        "id": "snap-fx-spy", "as_of": to_db(at - dt.timedelta(minutes=12)), "kinds": "[]",
        "subjects": json.dumps(["SPY"]), "entry_ids": json.dumps(["ctx-fx-regime-spy"]),
        "run_id": runs["director"], "created_at": to_db(at - dt.timedelta(minutes=12)),
    })  # fmt: skip
    # persona calls
    calls: dict[str, str] = {}
    for i, (persona, model, tin, tout, ms, usd) in enumerate([
        ("director", "claude-opus-5.5", 18400, 2100, 21400, 0.4335),
        ("quant", "claude-sonnet-5", 9600, 1500, 11800, 0.0513),
        ("risk", "claude-sonnet-5", 7200, 900, 8300, 0.0351),
    ]):  # fmt: skip
        pid = f"pc-fx-{persona}"
        prompt = (
            f"You are the Arc {persona.capitalize()}. Context snapshot snap-fx-spy: SPY bull ..."
        )
        _ins(conn, "persona_calls", {
            "id": pid, "run_id": runs[persona], "persona": persona, "model": model,
            "snapshot_id": "snap-fx-spy",
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "raw_response": "{}", "status": "ok", "error": None, "dropped": "{}",
            "created_at": to_db(at - dt.timedelta(minutes=9 - 3 * i)), "prompt_text": prompt,
            "prompt_inputs": "{}", "input_tokens": tin, "output_tokens": tout,
            "latency_ms": ms, "cost_usd": usd,
        })  # fmt: skip
        calls[persona] = pid
    # decision trail (chain rows + this trade's rows)
    t = at - dt.timedelta(minutes=12)
    common = {"chain": chain, "snap": "snap-fx-spy"}
    n = 0

    def dec(minutes: float, **kw: Any) -> str:
        nonlocal n
        n += 1
        return _decision(conn, n, at=t + dt.timedelta(minutes=minutes), **{**common, **kw})

    dec(0, persona="scout", stage="candidate", subject="SPY", choice="selected",
        code="scout_candidate", text="Three sources point to a breakout above 660.",
        run=runs["scout"], confidence=0.7)  # fmt: skip
    dec(3, persona="director", stage="shortlist", subject="market", choice="noted",
        code="market_read", text="Risk-on tape; vol cheap vs realised.",
        run=runs["director"], call=calls["director"])  # fmt: skip
    dec(3.1, persona="director", stage="shortlist", subject="SPY", choice="selected",
        code="shortlisted", text="Rank 1: cleanest trend, liquid chain.",
        run=runs["director"], call=calls["director"], confidence=0.72)  # fmt: skip
    dec(3.2, persona="director", stage="shortlist", subject="XLU", choice="rejected",
        code="not_ranked", text="Not this ticker: must not appear in SPY's trail.",
        run=runs["director"], call=calls["director"])  # fmt: skip
    dec(6, persona="quant", stage="structure", subject="SPY", choice="selected",
        code="chosen_from_menu", text="Debit call vertical 660/670: best net EV per $ risked.",
        run=runs["quant"], call=calls["quant"], h=spy_hash, confidence=0.66)  # fmt: skip
    dec(9, persona="risk", stage="risk_review", subject="SPY", choice="assessed",
        code="risk_assessed", text="Defined risk 1.2% of equity; no concentration.",
        run=runs["risk"], call=calls["risk"], h=spy_hash, confidence=0.7)  # fmt: skip
    dec(11, persona="sizing", stage="sizing", subject="SPY", choice="sized",
        code="sizing:ok", text="3 contracts (Risk's suggestion, under the 5% cap).",
        run=runs["risk"], h=spy_hash)  # fmt: skip
    dec(11.5, persona="system", stage="propose", subject="SPY", choice="selected",
        code="proposed", run=runs["risk"], h=spy_hash)  # fmt: skip
    dec(12.3, persona="gate", stage="gate", subject="SPY", choice="passed", code="gate:pass",
        run=runs["risk"], h=spy_hash)  # fmt: skip
    dec(14, persona="owner", stage="approval", subject="SPY", choice="approved",
        code="owner_approve", text="Approved in Slack.", h=spy_hash)  # fmt: skip
    dec(17, persona="system", stage="order", subject="SPY", choice="filled",
        code="order:filled", text="Filled 3 at 4.15 on step 1.", h=spy_hash)  # fmt: skip
    # full market context (append-only table: a later row supersedes the stub)
    mc_at = at + dt.timedelta(seconds=1)
    _ins(conn, "market_contexts", {
        "id": f"mc-full-{spy_hash[:12]}", "proposal_hash": spy_hash,
        "payload": _full_market_context(spy_hash, spy_structure, spot=663.40, at=mc_at),
        "quotes_as_of": to_db(mc_at), "created_at": to_db(mc_at),
    })  # fmt: skip
    # gate account snapshot on the SPY gate row (its real arc2 token is minted by _execute)
    conn.execute(
        "UPDATE gate_decisions SET account_snapshot = ?, run_id = ? WHERE proposal_hash = ?",
        (json.dumps({"equity": 100000, "open_positions": 2,
         "options_buying_power": 88000, "halted": False}), runs["risk"], spy_hash),
    )  # fmt: skip
    conn.execute(
        "UPDATE approval_requests SET channel = 'C0FIXTURE', thread_ts = '1790000000.000100', "
        "message_ts = '1790000000.000200', proposal_json = ? WHERE proposal_hash = ?",
        (json.dumps({"limit_price": "4.20"}), spy_hash),
    )
    # the D27 run manifest of the proposing run
    from arc.routines.manifest import RunManifest

    manifest = RunManifest(
        run_id=runs["risk"], job="risk", job_kind="persona", chain_run_id=chain, step_index=3,
        attempt=1, reason=f"chain:{runs['scout']}", scheduled_for=at - dt.timedelta(minutes=3),
        tick_now=at - dt.timedelta(minutes=3), started_at=at - dt.timedelta(minutes=3),
        finished_at=at - dt.timedelta(minutes=1), duration_ms=120_000, market_session="open",
        trading_day=at.date(), status="ok", arc_env="paper", account_profile="cash_debit",
        arc_version="0.1.0", git_sha=GIT_SHA, git_dirty=False, python="3.12.9",
        host="fixture", config_hashes={"routines.yaml": "a1b2c3d4", "exits.yaml": "e5f6a7b8",
                                       "costs.yaml": "c9d0e1f2"},
        config_version="7", effective_spec={}, declared_reads=["shortlist"],
        declared_writes=["risk_review"], kind_schema_versions={"shortlist": 3},
        snapshot_ids=["snap-fx-spy"], input_counts={"shortlist": 1}, input_digest="d" * 64,
        output_ids={"proposals": [spy_hash]}, persona_call_ids=[calls["risk"]],
        models_requested=["claude-sonnet-5"], models_served=["claude-sonnet-5"],
        input_tokens=7200, output_tokens=900, cost_usd=0.0351, proposal_hashes=[spy_hash],
    )  # fmt: skip
    _ins(conn, "run_manifests", {
        "id": "rm-fx-risk", "run_id": runs["risk"], "attempt": 1, "job": "risk",
        "chain_run_id": chain, "status": "ok", "schema_version": 1,
        "payload": manifest.model_dump_json(), "created_at": to_db(at - dt.timedelta(minutes=1)),
    })  # fmt: skip

    # -- AMD: close-to-reallocate pair, outcome, review --------------------------------
    closed_at = now - dt.timedelta(days=1)
    close_at = closed_at - dt.timedelta(minutes=20)
    amd_st = conn.execute(
        "SELECT structure_json FROM open_structures WHERE id = ?", (amd_structure_id,)
    ).fetchone()[0]
    from arc.models import Structure

    amd_structure = Structure.model_validate_json(amd_st)
    swap_id = "swap-fx-amd-xle"
    h_close = make_proposal(
        "exit-amd", "AMD", _closing(amd_structure, close_net=Decimal("-5.10")), at=close_at,
        contracts=2, kind="close", ev="0",
        pop=0.5, net_ev=None,
    )  # fmt: skip
    h_open = make_proposal("p-xle-swap", "XLE", vert("XLE", 95), at=close_at + dt.timedelta(
        minutes=6), contracts=2, pop=0.61, net_ev=31.5)  # fmt: skip
    conn.execute(
        "UPDATE proposals SET swap_id = ? WHERE proposal_hash IN (?, ?)", (swap_id, h_close, h_open)
    )
    make_gate(h_close, close_at)
    make_approval(h_close, "AMD", close_at, "approved")
    make_execute(h_close, at=close_at, kind="close", contracts=2, status="filled",
                 fill=Decimal("-5.10"))  # fmt: skip
    conn.execute(
        "UPDATE executions SET structure_id = ? WHERE proposal_hash = ?",
        (amd_structure_id, h_close),
    )
    _order_events(conn, h_close, close_at, "filled")
    conn.execute(
        "UPDATE open_structures SET exit_proposal_hash = ?, exit_reason = 'reallocate', "
        "exit_day = ? WHERE id = ?",
        (h_close, close_at.date().isoformat(), amd_structure_id),
    )
    make_gate(h_open, close_at + dt.timedelta(minutes=6))
    make_approval(h_open, "XLE", close_at + dt.timedelta(minutes=6), "expired")
    _ins(conn, "swaps", {
        "id": swap_id, "day": close_at.date().isoformat(), "status": "open_proposed",
        "detail": "AMD closed to fund XLE: +$18.20 net EV edge after costs",
        "close_structure_id": amd_structure_id, "close_ticker": "AMD",
        "close_proposal_hash": h_close, "open_ticker": "XLE", "source_ref": h_open,
        "open_proposal_hash": h_open,
        "suggestion_json": json.dumps({"edge": 18.2, "close_remaining_ev": 13.3,
                                       "open_net_ev": 31.5, "open_pop": 0.61}),
        "run_id": None, "created_at": to_db(close_at - dt.timedelta(minutes=1)),
        "updated_at": to_db(close_at + dt.timedelta(minutes=6)),
    })  # fmt: skip
    t = amd_opened - dt.timedelta(minutes=5)
    common.clear()
    d_amd = dec(0, persona="quant", stage="structure", subject="AMD", choice="selected",
                code="chosen_from_menu", text="Call debit spread into earnings.", h=amd_hash,
                confidence=0.62)  # fmt: skip
    t = close_at
    dec(0, persona="risk", stage="reallocate", subject="AMD", choice="approved",
        code="realloc:risk_approved", text="Swap edge +$18.20 beats the 10 $ minimum.",
        h=h_close)  # fmt: skip
    dec(0.5, persona="investor", stage="exit", subject="AMD", choice="selected",
        code="exit:reallocate", text="Close AMD to fund XLE.", h=h_close)  # fmt: skip
    dec(5, persona="system", stage="order", subject="AMD", choice="filled", code="order:filled",
        text="Closed 2 at 5.10 credit.", h=h_close)  # fmt: skip
    dec(6, persona="system", stage="propose", subject="XLE", choice="selected",
        code="realloc:open_proposed", h=h_open)  # fmt: skip
    _ins(conn, "outcomes", {
        "id": "out-fx-amd", "proposal_hash": amd_hash, "status": "closed", "contracts": 2,
        "limit_price": "3.60", "entry_fill": "3.60", "slippage_usd": "0.00",
        "slippage_bps": 0.0, "cost_bps": 35.0, "exit_fill": "-5.10", "realised_pnl": "300.00",
        "max_adverse_excursion": "-84.00", "days_held": 7, "exit_reason": "reallocate",
        "ev_total": "24.80", "pnl_vs_ev": "275.20", "hold_to_expiry_shadow_pnl": "412.00",
        "at": to_db(closed_at),
    })  # fmt: skip
    _ins(conn, "decision_reviews", {
        "id": "rev-fx-amd", "proposal_hash": amd_hash, "decision_id": None,
        "label": "good_decision_good_outcome", "root_cause": "exit_management",
        "notes": "Thesis played out; the swap freed buying power for a better trade.",
        "reviewer": "owner", "at": to_db(closed_at + dt.timedelta(hours=2)),
    })  # fmt: skip
    _ins(conn, "decision_review_citations", {"review_id": "rev-fx-amd", "decision_id": d_amd})
    return {"exit-amd": h_close, "p-xle-swap": h_open}
