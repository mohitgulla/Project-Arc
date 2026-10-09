"""E6.2g: ``arc journal repair-fill-signs`` restates fills stored with the wrong sign.

Before E6.2f a one-leg sell-to-close (a credit, band ``-43.62 .. -42.53``) was
stored as ``+43.45``. These tests build a correct ladder open + close, then rewrite
the close the way the pre-fix adapter stored it (fill, structure, tax lot, journal
row, outcome), so they do not depend on the ladder's own sign handling.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.journal.fill_signs import find_sign_mismatches, repair_fill_signs
from arc.journal.outcomes import record_close_outcome
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.journal.scorecard import closed_positions, execution_costs
from arc.store.db import connect
from arc.store.execution import OpenStructureRepo
from arc.store.migrate import migrate
from tests import test_execution_submit as S
from tests import test_journal_outcomes as O

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

NOW = S.NOW + dt.timedelta(hours=1)
WINDOW = (S.NOW - dt.timedelta(days=1), S.NOW + dt.timedelta(days=1))


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _closed_pair(conn: sqlite3.Connection) -> str:
    """Open 2 at -0.85 (credit) and close both at +0.40 (debit): realised +90."""
    sid = O._open(conn)
    O._close(conn, sid, thesis="exit", qty=2, price="0.40")
    return sid


def _corrupt(conn: sqlite3.Connection, sid: str) -> None:
    """Store the close the pre-E6.2f way: the fill sign flipped (+0.40 -> -0.40).

    The journal tables are append-only (triggers), so the fixture lifts the
    ``decisions`` / ``outcomes`` triggers to forge the pre-fix rows, then restores them.
    """
    triggers = conn.execute(
        """SELECT name, sql FROM sqlite_master WHERE type = 'trigger'
           AND tbl_name IN ('decisions', 'outcomes')"""
    ).fetchall()
    for name, _ in triggers:
        conn.execute(f"DROP TRIGGER {name}")
    (phash,) = conn.execute(
        "SELECT proposal_hash FROM executions WHERE structure_id = ? AND kind = 'close'", (sid,)
    ).fetchone()
    conn.execute("UPDATE executions SET fill_price = '-0.40' WHERE proposal_hash = ?", (phash,))
    conn.execute(
        """UPDATE fills SET price = '-0.40'
           WHERE order_id IN (SELECT id FROM orders WHERE proposal_hash = ?)""",
        (phash,),
    )
    conn.execute("UPDATE open_structures SET close_net = '-0.40' WHERE id = ?", (sid,))
    # -(-0.85 + -0.40) x 100 x 2 = +250 instead of +90
    conn.execute(
        """UPDATE tax_lots SET realized_pnl = '250.00'
           WHERE realized_pnl IS NOT NULL AND CAST(realized_pnl AS REAL) != 0"""
    )
    conn.execute(
        """UPDATE decisions SET payload = json_set(payload, '$.realized_pnl', '250.00')
           WHERE reason_code = 'exit:closed'"""
    )
    conn.execute("DELETE FROM outcomes")
    for _, sql in triggers:
        conn.execute(sql)
    conn.commit()
    record_close_outcome(conn, sid)
    conn.commit()


def _bad(conn: sqlite3.Connection) -> str:
    sid = _closed_pair(conn)
    _corrupt(conn, sid)
    return sid


def _realised(conn: sqlite3.Connection, sid: str) -> float:
    (c,) = [
        c for c in closed_positions(conn, start=WINDOW[0], end=WINDOW[1], now=NOW)
        if c.structure_id == sid
    ]  # fmt: skip
    return c.realised_pnl


def _latest_outcome(conn: sqlite3.Connection) -> dict[str, object]:
    return dict(conn.execute("SELECT * FROM outcomes ORDER BY at DESC, rowid DESC").fetchone())


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def test_detection_ignores_correctly_signed_rows(conn: sqlite3.Connection) -> None:
    _closed_pair(conn)  # open -0.85 in a credit band, close +0.40 in a debit band
    assert find_sign_mismatches(conn) == []
    assert repair_fill_signs(conn, now=NOW) == []


def test_detection_finds_only_the_flipped_fill(conn: sqlite3.Connection) -> None:
    sid = _bad(conn)
    (f,) = find_sign_mismatches(conn)
    assert (f.structure_id, f.kind, f.old_fill, f.new_fill) == (sid, "close", D("-0.40"), D("0.40"))


def test_detection_skips_a_band_straddling_zero(conn: sqlite3.Connection) -> None:
    sid = _bad(conn)
    conn.execute(
        "UPDATE executions SET band_lo = '-0.10', band_hi = '0.10' "
        "WHERE structure_id = ? AND kind = 'close'",
        (sid,),
    )
    assert find_sign_mismatches(conn) == []


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def test_repair_restates_every_row_once(conn: sqlite3.Connection) -> None:
    sid = _bad(conn)
    assert _realised(conn, sid) == 250.0
    old_outcome = _latest_outcome(conn)
    assert D(str(old_outcome["realised_pnl"])) == D("250")

    (rep,) = repair_fill_signs(conn, now=NOW)
    assert rep.structure_id == sid and rep.realised == (D("250"), D("90"))
    assert rep.delta == D("-160")

    close = conn.execute(
        "SELECT proposal_hash, fill_price FROM executions "
        "WHERE structure_id = ? AND kind = 'close'",
        (sid,),
    ).fetchone()
    assert D(close[1]) == D("0.40")
    prices = {
        D(r[0])
        for r in conn.execute(
            "SELECT f.price FROM fills f JOIN orders o ON o.id = f.order_id "
            "WHERE o.proposal_hash = ?",
            (close[0],),
        )
    }
    assert prices == {D("0.40")}
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None and D(row["close_net"]) == D("0.40")
    assert D(row["entry_net"]) == D("-0.85")  # the correctly signed open is untouched
    lots = [D(r[0]) for r in conn.execute("SELECT realized_pnl FROM tax_lots ORDER BY rowid")]
    assert sum(lots) == D("90")
    # the outcome is re-derived as a new row superseding the stored one
    new = _latest_outcome(conn)
    assert new["supersedes_id"] == old_outcome["id"]
    assert D(str(new["realised_pnl"])) == D("90") and D(str(new["exit_fill"])) == D("-0.40")
    # the exit:closed row stays as written; one correction decision is appended
    (closed_payload,) = [
        json.loads(r[0])
        for r in conn.execute("SELECT payload FROM decisions WHERE reason_code = 'exit:closed'")
    ]
    assert closed_payload["realized_pnl"] == "250.00"
    (fix,) = [
        json.loads(r[0])
        for r in conn.execute(
            "SELECT payload FROM decisions WHERE reason_code = ?",
            (str(ReasonCode.RECONCILE_FILL_SIGN),),
        )
    ]
    assert fix["structure_id"] == sid
    assert (fix["old_realized_pnl"], fix["new_realized_pnl"], fix["realized_pnl_delta"]) == (
        "250.00",
        "90.00",
        "-160.00",
    )
    # the scorecard / Tower read the corrected number
    assert _realised(conn, sid) == 90.0


def test_repair_is_idempotent(conn: sqlite3.Connection) -> None:
    sid = _bad(conn)
    assert len(repair_fill_signs(conn, now=NOW)) == 1
    snapshot = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]  # noqa: S608
        for t in ("executions", "fills", "open_structures", "tax_lots", "outcomes", "decisions")
    }
    assert repair_fill_signs(conn, now=NOW) == []
    for t, rows in snapshot.items():
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] == rows  # noqa: S608
    n = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE reason_code = ?",
        (str(ReasonCode.RECONCILE_FILL_SIGN),),
    ).fetchone()[0]
    assert n == 1
    assert _realised(conn, sid) == 90.0


def test_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    sid = _bad(conn)
    before = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]  # noqa: S608
        for t in ("executions", "fills", "open_structures", "tax_lots", "outcomes", "decisions")
    }
    (rep,) = repair_fill_signs(conn, now=NOW, dry_run=True)
    assert rep.realised == (D("250"), D("90")) and rep.decision_id is None
    assert rep.outcome == ("250.00", "90.00")
    for t, rows in before.items():
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] == rows  # noqa: S608
    assert _realised(conn, sid) == 250.0
    assert not conn.in_transaction


def test_flipped_open_restates_entry_net(conn: sqlite3.Connection) -> None:
    """Generalised: an open fill stored against its band's sign is restated too."""
    sid = O._open(conn)  # -0.85 in the credit band
    conn.execute("UPDATE executions SET fill_price = '0.85' WHERE kind = 'open'")
    conn.execute("UPDATE open_structures SET entry_net = '0.85' WHERE id = ?", (sid,))
    conn.commit()
    (rep,) = repair_fill_signs(conn, now=NOW)
    assert rep.entry_net == (D("0.85"), D("-0.85")) and rep.realised is None
    row = OpenStructureRepo(conn).get(sid)
    assert row is not None and D(row["entry_net"]) == D("-0.85")
    assert repair_fill_signs(conn, now=NOW) == []


def test_costs_read_the_corrected_fill(conn: sqlite3.Connection) -> None:
    _bad(conn)
    bad = execution_costs(conn, *WINDOW)
    repair_fill_signs(conn, now=NOW)
    good = execution_costs(conn, *WINDOW)
    (bad_close,) = [r for r in bad.rows if r.kind == "close"]
    (good_close,) = [r for r in good.rows if r.kind == "close"]
    # a 0.80 sign flip on 2 contracts moved fill-vs-mid by $160 (here in the
    # favourable direction: the debit close was stored as a credit)
    assert good_close.realised_usd - bad_close.realised_usd == pytest.approx(160.0)


def test_reason_label() -> None:
    assert REASON_LABELS[ReasonCode.RECONCILE_FILL_SIGN]


def test_cli_dry_run_then_run_then_noop(
    conn: sqlite3.Connection,
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    from arc.cli import main

    db = tmp_path / "arc.db"
    disk = connect(str(db))
    migrate(disk)
    _bad(disk)
    disk.close()
    assert main(["journal", "repair-fill-signs", "--dry-run", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "realised +250.00 -> +90.00 (delta -160.00)" in out
    assert "dry run: would change 1 structure(s), 1 execution(s)" in out
    assert main(["journal", "repair-fill-signs", "--db", str(db)]) == 0
    assert "changed 1 structure(s)" in capsys.readouterr().out
    assert main(["journal", "repair-fill-signs", "--db", str(db)]) == 0
    assert "changed 0 structure(s), 0 execution(s)" in capsys.readouterr().out
