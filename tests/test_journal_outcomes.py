"""E7.4b: ``outcomes`` rows are written on close / expiry, idempotently, plus the backfill.

Paths: a full ladder close, a reconcile expiry settlement, a partial close then a
full close (one outcome, original size, blended exit), an idempotent rerun, the
``arc journal backfill-outcomes`` CLI, and the readers that must count the rows
(the Analyst gate's ``closed_filter`` and ``arc scorecard attribution``).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from arc.gate import proposal_hash
from arc.gate.band import PriceBand
from arc.journal.models import OutcomeRecord, OutcomeStatus
from arc.journal.outcomes import backfill_outcomes, build_close_outcome, record_close_outcome
from arc.journal.store import JournalStore
from arc.journal.views import attribution
from arc.models import QuantMetrics, Sizing
from arc.store.db import connect
from arc.store.execution import OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import ProposalRepo
from tests import test_execution_ladder as L
from tests import test_execution_submit as S
from tests import test_reconcile as R

if TYPE_CHECKING:
    import sqlite3

    from arc.models import Proposal

QUANT = QuantMetrics(pop=0.6, ev=D("12.5"), cost_bps=40.0)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def outcome_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return [dict(r) for r in conn.execute("SELECT * FROM outcomes ORDER BY rowid")]


def _close_proposal(conn: sqlite3.Connection, thesis: str, contracts: int, price: str) -> Proposal:
    p = S.proposal(
        thesis=thesis,
        limit_price=D(price),
        sizing=Sizing(contracts=contracts, notional=D("100"), pct_equity=0.01),
        expires_at=S.NOW + dt.timedelta(minutes=20),
    )
    ProposalRepo(conn).insert(
        candidate_id=p.candidate_id,
        proposal_hash=proposal_hash(p),
        structure_json=p.structure.model_dump_json(),
        thesis=p.thesis,
        quant_json=p.quant.model_dump_json(),
        sizing_json=p.sizing.model_dump_json(),
        expires_at=p.expires_at.isoformat(),
        ticker="SPY",
        kind="close",
    )
    return p


def _open(conn: sqlite3.Connection) -> str:
    """Open 2 contracts at -0.85 (credit) through the ladder; returns the structure id."""
    b = L.ScriptedBroker([["filled"]], fills={0: (2, "-0.85")})
    opened = L.run(conn, b)
    assert opened.structure_id is not None
    return opened.structure_id


def _close(conn: sqlite3.Connection, sid: str, *, thesis: str, qty: int, price: str) -> None:
    p = _close_proposal(conn, thesis, qty, price)
    band = PriceBand(lo=D(price), hi=D(price), max_steps=0)
    b = L.ScriptedBroker([["filled"]], fills={0: (qty, price)})
    out = L.run(conn, b, p=p, decision=L.gated(p, band=band), kind="close", structure_id=sid)
    assert out.filled_qty == qty


# ---------------------------------------------------------------------------
# ladder close
# ---------------------------------------------------------------------------


def test_full_close_writes_one_closed_outcome(conn: sqlite3.Connection) -> None:
    sid = _open(conn)
    assert outcome_rows(conn) == []
    _close(conn, sid, thesis="exit", qty=2, price="0.40")
    (o,) = outcome_rows(conn)
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None
    assert o["proposal_hash"] == row["open_proposal_hash"]
    assert o["status"] == "closed" and o["contracts"] == 2
    assert D(str(o["entry_fill"])) == D("-0.85")
    # closing a credit spread for a 0.40 debit: position value -0.40
    assert D(str(o["exit_fill"])) == D("-0.40")
    assert D(str(o["realised_pnl"])) == D("90")  # (0.85 - 0.40) x 100 x 2, = the ladder's
    assert o["days_held"] == 0 and o["supersedes_id"] is None
    assert D(str(o["ev_total"])) == D("2")  # S.proposal quant.ev 1 x 2 contracts
    # the outcome landed in the same commit as the close
    assert not conn.in_transaction


def test_close_outcome_failure_never_blocks_the_close(conn: sqlite3.Connection) -> None:
    sid = _open(conn)
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None
    conn.execute(
        "UPDATE proposals SET quant_json = '{}' WHERE proposal_hash = ?",
        (row["open_proposal_hash"],),
    )
    conn.commit()
    _close(conn, sid, thesis="exit", qty=2, price="0.40")
    assert OpenStructureRepo(conn).get(sid)["status"] == "closed"  # type: ignore[index]
    assert outcome_rows(conn) == []  # logged as journal.outcome_skipped


def test_partial_then_full_close_writes_one_blended_outcome(conn: sqlite3.Connection) -> None:
    sid = _open(conn)
    _close(conn, sid, thesis="exit-1", qty=1, price="0.40")
    assert outcome_rows(conn) == []  # still open with 1 contract
    assert OpenStructureRepo(conn).get(sid)["contracts"] == 1  # type: ignore[index]
    _close(conn, sid, thesis="exit-2", qty=1, price="0.20")
    (o,) = outcome_rows(conn)
    assert o["status"] == "closed" and o["contracts"] == 2  # the original size
    assert D(str(o["exit_fill"])) == D("-0.30")  # qty-weighted mean of -0.40 and -0.20
    # = the per-tranche P&L the ladder books: 45 + 65
    assert D(str(o["realised_pnl"])) == D("110")
    pnls = [
        D(json.loads(r[0])["realized_pnl"])
        for r in conn.execute("SELECT payload FROM decisions WHERE reason_code = 'exit:closed'")
    ]
    assert sum(pnls) == D("110")


def test_rerun_is_idempotent_and_supersedes_an_open_row(conn: sqlite3.Connection) -> None:
    sid = _open(conn)
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None
    phash = row["open_proposal_hash"]
    # an earlier `open` outcome is superseded, not duplicated
    open_id = JournalStore(conn).record_outcome(
        OutcomeRecord(proposal_hash=phash, status=OutcomeStatus.OPEN, at=S.NOW)
    )
    conn.commit()
    _close(conn, sid, thesis="exit", qty=2, price="0.40")
    rows = outcome_rows(conn)
    assert [r["status"] for r in rows] == ["open", "closed"]
    assert rows[1]["supersedes_id"] == open_id
    # every rerun path is a no-op once a closed row exists
    assert record_close_outcome(conn, sid) is None
    assert [r.action for r in backfill_outcomes(conn)] == ["exists"]
    assert len(outcome_rows(conn)) == 2
    assert JournalStore(conn).outcome(phash).status == OutcomeStatus.CLOSED  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# reconcile expiry
# ---------------------------------------------------------------------------


def _with_quant(conn: sqlite3.Connection, phash: str, contracts: int = 2) -> None:
    conn.execute(
        "UPDATE proposals SET quant_json = ?, sizing_json = ? WHERE proposal_hash = ?",
        (
            QUANT.model_dump_json(),
            Sizing(contracts=contracts, notional=D("200"), pct_equity=0.01).model_dump_json(),
            phash,
        ),
    )
    conn.commit()


def test_expiry_settlement_writes_outcome_with_shadow(conn: sqlite3.Connection) -> None:
    exp = dt.date(2026, 9, 25)
    pos = R.open_position(
        conn, st=R.bull_put(exp), opened=R.OPENED - dt.timedelta(days=10), entry="-0.90"
    )
    _with_quant(conn, pos["phash"])
    rep = R.run(conn, R.FakeBroker(), settle_price=lambda root, day: D("710.50"))
    assert rep.expired == [pos["sid"]]
    (o,) = outcome_rows(conn)
    assert o["status"] == "closed" and o["exit_reason"] == "expiry"
    assert D(str(o["exit_fill"])) == D("-0.50")  # short 711 put 0.50 ITM
    assert D(str(o["realised_pnl"])) == D("80")  # = the reconcile's realized
    assert D(str(o["hold_to_expiry_shadow_pnl"])) == D("80")  # D19: held to expiry
    assert o["days_held"] == 10
    assert D(str(o["ev_total"])) == D("25")  # 12.5 x 2


def test_expiry_worthless(conn: sqlite3.Connection) -> None:
    exp = dt.date(2026, 9, 25)
    pos = R.open_position(
        conn, st=R.bull_put(exp), opened=R.OPENED - dt.timedelta(days=10), entry="-0.90"
    )
    _with_quant(conn, pos["phash"])
    R.run(conn, R.FakeBroker(), settle_price=lambda root, day: D("720"))
    (o,) = outcome_rows(conn)
    assert o["status"] == "expired_worthless" and o["exit_fill"] == "0"
    assert D(str(o["realised_pnl"])) == D("180")  # kept the 0.90 credit x 2


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def _closed_without_outcome(conn: sqlite3.Connection) -> str:
    """A structure closed through the ladder before E7.4b: drop the outcome it wrote."""
    sid = _open(conn)
    conn.execute("DROP TRIGGER outcomes_no_delete")
    _close(conn, sid, thesis="exit", qty=2, price="0.40")
    conn.execute("DELETE FROM outcomes")
    conn.commit()
    return sid


def test_backfill_writes_missing_outcomes_once(conn: sqlite3.Connection) -> None:
    sid = _closed_without_outcome(conn)
    dry = backfill_outcomes(conn, dry_run=True)
    assert [(r.structure_id, r.action, r.status) for r in dry] == [(sid, "would_write", "closed")]
    assert outcome_rows(conn) == []
    (res,) = backfill_outcomes(conn)
    assert res.action == "written" and D(str(res.realised_pnl)) == D("90")
    assert D(str(res.exit_fill)) == D("-0.40")
    assert [r.action for r in backfill_outcomes(conn)] == ["exists"]
    (o,) = outcome_rows(conn)
    assert o["exit_reason"] != "expiry" and o["status"] == "closed"


def test_backfill_matches_the_live_writer(conn: sqlite3.Connection) -> None:
    """Partial + full close and an expiry, outcomes dropped: backfill rebuilds the same rows."""
    sid = _open(conn)
    _close(conn, sid, thesis="exit-1", qty=1, price="0.40")
    _close(conn, sid, thesis="exit-2", qty=1, price="0.20")
    exp = dt.date(2026, 9, 25)
    pos = R.open_position(
        conn,
        phash="b" * 64,
        st=R.bull_put(exp),
        opened=R.OPENED - dt.timedelta(days=10),
        entry="-0.90",
    )
    _with_quant(conn, pos["phash"])
    R.run(conn, R.FakeBroker(), settle_price=lambda root, day: D("710.50"))
    keys = ("proposal_hash", "status", "contracts", "exit_fill", "realised_pnl", "exit_reason")
    live = sorted(tuple(o[k] for k in keys) for o in outcome_rows(conn))
    assert len(live) == 2
    conn.execute("DROP TRIGGER outcomes_no_delete")
    conn.execute("DELETE FROM outcomes")
    conn.commit()
    assert sorted(r.action for r in backfill_outcomes(conn)) == ["written", "written"]
    rebuilt = sorted(tuple(o[k] for k in keys) for o in outcome_rows(conn))
    # the backfill has no settlement price, so only the D19 shadow differs (not compared)
    assert rebuilt == live


def test_backfill_skips_unbuildable_and_ignores_open(conn: sqlite3.Connection) -> None:
    sid = _closed_without_outcome(conn)
    R.open_position(conn)  # open: not a backfill target
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None
    conn.execute(
        "UPDATE proposals SET quant_json = '{}' WHERE proposal_hash = ?",
        (row["open_proposal_hash"],),
    )
    (res,) = backfill_outcomes(conn)
    assert res.action == "skipped" and res.detail
    assert outcome_rows(conn) == []


def test_build_close_outcome_rejects_open_and_unknown(conn: sqlite3.Connection) -> None:
    pos = R.open_position(conn)
    with pytest.raises(ValueError, match="not closed"):
        build_close_outcome(conn, pos["sid"])
    with pytest.raises(LookupError):
        build_close_outcome(conn, "os-missing")
    assert record_close_outcome(conn, "os-missing") is None
    assert record_close_outcome(conn, pos["sid"]) is None  # open: skipped, nothing written
    assert outcome_rows(conn) == []


def test_backfill_cli(conn: sqlite3.Connection, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from arc.cli import main

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    _closed_without_outcome(c)
    c.close()
    assert main(["journal", "backfill-outcomes", "--dry-run", "--db", str(db)]) == 0
    assert "dry run: would_write=1" in capsys.readouterr().out
    assert main(["journal", "backfill-outcomes", "--db", str(db)]) == 0
    assert "written=1" in capsys.readouterr().out
    assert main(["journal", "backfill-outcomes", "--json", "--db", str(db)]) == 0
    (res,) = json.loads(capsys.readouterr().out)
    assert res["action"] == "exists" and res["status"] == "closed"


# ---------------------------------------------------------------------------
# readers count them
# ---------------------------------------------------------------------------


def _analyst() -> object:
    path = Path(__file__).resolve().parents[1] / "hermes" / "analyst" / "arc_analyst.py"
    spec = importlib.util.spec_from_file_location("arc_analyst_e74b", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_analyst_gate_and_attribution_count_the_outcome(conn: sqlite3.Connection) -> None:
    sid = _open(conn)
    _close(conn, sid, thesis="exit", qty=2, price="0.40")
    closed_filter = _analyst().closed_filter  # type: ignore[attr-defined]
    n = conn.execute(f"SELECT COUNT(*) FROM outcomes WHERE {closed_filter()}").fetchone()[0]
    assert n == 1
    rep = attribution(conn, since=None, until=S.NOW + dt.timedelta(days=1))
    assert rep.trades == 1 and rep.rows[0].realised_pnl == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# E18.3: MFE from the stored marks, the shadow + MFE backfill after expiry
# ---------------------------------------------------------------------------


def _review(conn: sqlite3.Connection, sid: str, value: str, at: dt.datetime) -> None:
    from arc.context.ttl import to_db

    conn.execute(
        "INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by, "
        "created_at, valid_from, status) "
        "VALUES (?, 'position_review', ?, ?, 3, 'positions.evaluate', ?, ?, 'superseded')",
        (f"ctx-{sid}-{at.timestamp()}", sid,
         json.dumps({"current_value": float(value), "entry_net": -0.85}),
         to_db(at), to_db(at)),
    )  # fmt: skip


def _closed_with_marks(conn: sqlite3.Connection) -> str:
    """Credit spread opened at -0.85 two days early, marked -1.10 then -0.30, closed at 0.40."""
    from arc.context.ttl import to_db

    sid = _open(conn)
    t0 = S.NOW - dt.timedelta(days=2)
    conn.execute("UPDATE open_structures SET opened_at = ? WHERE id = ?", (to_db(t0), sid))
    _review(conn, sid, "-1.10", t0 + dt.timedelta(hours=1))  # -0.25/share: -50 on 2
    _review(conn, sid, "-0.30", t0 + dt.timedelta(days=1))  # +0.55/share: +110 on 2
    _review(conn, sid, "-0.05", S.NOW + dt.timedelta(days=1))  # after the close: ignored
    conn.commit()
    _close(conn, sid, thesis="exit", qty=2, price="0.40")
    return sid


def test_close_outcome_mae_and_mfe_use_the_stored_marks(conn: sqlite3.Connection) -> None:
    _closed_with_marks(conn)
    (o,) = outcome_rows(conn)
    assert D(str(o["realised_pnl"])) == D("90")
    assert D(str(o["max_adverse_excursion"])) == D("-50")
    assert D(str(o["max_favourable_excursion"])) == D("110")  # the peak mark, not the exit
    assert o["hold_to_expiry_shadow_pnl"] is None  # closed before expiry: unknown yet
    rec = JournalStore(conn).outcome(str(o["proposal_hash"]))
    assert rec is not None and rec.max_favourable_excursion == D("110")


def test_backfill_exit_stats_prices_the_shadow_after_expiry(conn: sqlite3.Connection) -> None:
    from arc.journal.outcomes import backfill_exit_stats

    sid = _closed_with_marks(conn)
    exp = S.EXP  # 2026-11-20
    calls: list[tuple[str, dt.date]] = []

    def settle(root: str, day: dt.date) -> D:
        calls.append((root, day))
        return D("572")  # above both puts: the credit spread expires worthless

    # before expiry: nothing to price, MFE already there -> pending
    (r,) = backfill_exit_stats(conn, today=exp, settle_price=settle)
    assert r.action == "pending_expiry" and r.structure_id == sid and calls == []
    assert len(outcome_rows(conn)) == 1
    # after expiry, dry run: builds, writes nothing
    (r,) = backfill_exit_stats(conn, today=exp + dt.timedelta(days=1), settle_price=settle,
                               dry_run=True)  # fmt: skip
    assert r.action == "would_write" and D(str(r.shadow)) == D("170") and calls == [("SPY", exp)]
    assert len(outcome_rows(conn)) == 1
    # after expiry: a superseding row with the D19 shadow (kept the 0.85 credit x 2)
    (r,) = backfill_exit_stats(conn, today=exp + dt.timedelta(days=1), settle_price=settle)
    assert r.action == "written" and D(str(r.mfe)) == D("110")
    first, second = outcome_rows(conn)
    assert second["supersedes_id"] == first["id"]
    assert D(str(second["hold_to_expiry_shadow_pnl"])) == D("170")
    assert D(str(second["realised_pnl"])) == D("90")  # the realised result is unchanged
    # idempotent
    (r,) = backfill_exit_stats(conn, today=exp + dt.timedelta(days=5), settle_price=settle)
    assert r.action == "complete" and len(outcome_rows(conn)) == 2


def test_backfill_exit_stats_fills_mfe_of_old_rows_and_waits_for_a_settle(
    conn: sqlite3.Connection,
) -> None:
    from arc.journal.outcomes import backfill_exit_stats

    _closed_with_marks(conn)
    # a pre-E18.3 row: no MFE column value
    conn.execute("DROP TRIGGER outcomes_no_update")  # simulate a pre-E18.3 row
    conn.execute("UPDATE outcomes SET max_favourable_excursion = NULL")
    conn.commit()
    late = S.EXP + dt.timedelta(days=1)
    (r,) = backfill_exit_stats(conn, today=late, settle_price=lambda root, day: None)
    assert r.action == "written" and D(str(r.mfe)) == D("110") and r.shadow is None
    (r,) = backfill_exit_stats(conn, today=late, settle_price=lambda root, day: None)
    assert r.action == "no_settle"  # MFE done, settlement close not known
    (r,) = backfill_exit_stats(conn, today=late, settle_price=None)
    assert r.action == "no_settle"
    assert len(outcome_rows(conn)) == 2


def test_backfill_exit_stats_skips_unbuildable(conn: sqlite3.Connection) -> None:
    from arc.journal.outcomes import backfill_exit_stats

    sid = _closed_without_outcome(conn)
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None
    conn.execute(
        "UPDATE proposals SET quant_json = '{}' WHERE proposal_hash = ?",
        (row["open_proposal_hash"],),
    )
    (r,) = backfill_exit_stats(conn, today=S.NOW.date())
    assert r.action == "skipped" and r.detail
    assert outcome_rows(conn) == []


def test_backfill_exit_stats_cli(conn: sqlite3.Connection, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from arc.cli import main

    db = tmp_path / "arc.db"
    c = connect(str(db))
    migrate(c)
    _closed_with_marks(c)
    c.execute("DROP TRIGGER outcomes_no_update")
    c.execute("UPDATE outcomes SET max_favourable_excursion = NULL")
    c.commit()
    c.close()
    args = ["journal", "backfill-exit-stats", "--no-settle", "--db", str(db)]
    assert main([*args, "--dry-run"]) == 0
    assert "dry run: would_write=1" in capsys.readouterr().out
    assert main(args) == 0
    assert "written=1" in capsys.readouterr().out
    assert main([*args, "--json"]) == 0
    (res,) = json.loads(capsys.readouterr().out)
    assert D(res["mfe"]) == D("110") and res["action"] in {"pending_expiry", "no_settle"}


def test_nightly_reconcile_prices_the_shadow_of_an_expired_early_close(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.broker import reconcile_job

    _closed_with_marks(conn)
    ctx = R._ctx(conn)
    monkeypatch.setattr(ctx, "now", dt.datetime(2026, 11, 23, 20, 0, tzinfo=dt.UTC))
    seen: list[dt.date] = []

    def settle(root: str, day: dt.date) -> D:
        seen.append(day)
        return D("572")

    reconcile_job._fill_exit_stats(ctx, settle)
    assert seen == [S.EXP]
    assert D(str(outcome_rows(conn)[-1]["hold_to_expiry_shadow_pnl"])) == D("170")

    def boom(root: str, day: dt.date) -> D:
        raise RuntimeError("feed down")

    conn.execute("DROP TRIGGER outcomes_no_delete")
    conn.execute("DELETE FROM outcomes WHERE supersedes_id IS NOT NULL")
    conn.commit()
    reconcile_job._fill_exit_stats(ctx, boom)  # never raises into the reconcile
    assert len(outcome_rows(conn)) == 1
