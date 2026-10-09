"""Repair stored fills whose sign disagrees with their signed band (E6.2g).

Before E6.2f the Alpaca adapter returned a single-leg fill unsigned, so a
sell-to-close (a credit, band ``-43.62 .. -42.53``) was stored as ``+43.45``: the
close looked like a debit and the realised P&L of the structure was inflated by
``2 x |fill| x 100 x qty``. :func:`repair_fill_signs` finds those rows
deterministically and restates them; ``arc journal repair-fill-signs`` is the CLI.

Detection. An ``executions`` row with ``filled_qty > 0`` whose band is one-signed
(``band_lo`` and ``band_hi`` both > 0 or both < 0) and whose ``fill_price`` is
non-zero with the opposite sign. Bands that straddle zero are ambiguous and never
touched; multi-leg and correctly signed rows match nothing.

Repair, one transaction per structure:

1. negate ``executions.fill_price`` and every ``fills.price`` of the execution's
   orders that has the same wrong sign;
2. ``open_structures.close_net`` (a close that is the structure's final tranche)
   or ``entry_net`` (an open) takes the corrected price;
3. the realised P&L, ``-(entry_net + close) x 100 x qty`` summed over the close
   tranches (:func:`arc.journal.outcomes._tranches`), is recomputed before and
   after; the delta goes on the structure's P&L-carrying tax lot
   (``tax_lots.realized_pnl``). ``tax_lots.close_price`` stays: lots hold the
   unsigned per-leg broker price (``reconcile._leg_prices``), which was right;
4. a closed structure's outcome is re-derived through
   :func:`arc.journal.outcomes.record_close_outcome` with ``restate=True``: a new
   row superseding the stored one;
5. the append-only journal gets one ``reconcile:fill_sign_corrected`` decision
   carrying ``structure_id``, the old and new realised P&L and
   ``realized_pnl_delta``; :func:`arc.journal.scorecard._realised_by_structure`
   adds that delta to the ``exit:closed`` rows it sums.

Then, after the structures: :func:`restate_pnl_snapshots` re-derives the
``realized`` / ``total`` of every production ``pnl_snapshots`` row (the reconcile's
day snapshot, read by the Tower's Day P&L card) on a day a corrected lot closed.
``realized`` is recomputed the way the reconcile computes it
(``reconcile.engine._realized_today``): the sum of ``tax_lots.realized_pnl`` closed
that ET day at or before the snapshot. A row changes only when the gap to the
stored value equals the corrections' summed delta, so a snapshot that drifted for
another reason is left alone (logged), and an already restated one shows no gap.
This step runs on every invocation, so it also repairs a store whose structures
were restated before the snapshot step existed.

Idempotent: after a run nothing mismatches, so a second run changes nothing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from arc.journal.outcomes import _final_close_hash, _tranches, record_close_outcome
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

__all__ = [
    "FillSignFix",
    "SnapshotRestatement",
    "StructureRepair",
    "find_sign_mismatches",
    "repair_fill_signs",
    "restate_pnl_snapshots",
]

log = structlog.get_logger(__name__)

_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class FillSignFix:
    """One execution whose stored fill sign disagrees with its band."""

    proposal_hash: str
    kind: str
    structure_id: str
    band_lo: Decimal
    band_hi: Decimal
    filled_qty: int
    old_fill: Decimal

    @property
    def new_fill(self) -> Decimal:
        return Decimal(0) - self.old_fill


@dataclass
class StructureRepair:
    """Everything one structure's repair changes (before -> after)."""

    structure_id: str
    ticker: str
    status: str
    fixes: list[FillSignFix]
    fills: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)  # id, old, new
    entry_net: tuple[Decimal, Decimal] | None = None
    close_net: tuple[Decimal, Decimal] | None = None
    realised: tuple[Decimal, Decimal] | None = None
    tax_lot: tuple[str, Decimal, Decimal] | None = None  # id, old, new realized_pnl
    outcome: tuple[str | None, str | None] | None = None  # old realised, new realised
    outcome_id: str | None = None
    decision_id: str | None = None

    @property
    def delta(self) -> Decimal:
        return Decimal(0) if self.realised is None else self.realised[1] - self.realised[0]

    def lines(self) -> list[str]:
        out = [f"{self.ticker:<6} {self.structure_id} ({self.status})"]
        for f in self.fixes:
            out.append(
                f"  executions {f.kind:<5} {f.proposal_hash[:12]} fill_price "
                f"{f.old_fill:+} -> {f.new_fill:+} (band {f.band_lo:+} .. {f.band_hi:+})"
            )
        out += [f"  fills {fid} price {o:+} -> {n:+}" for fid, o, n in self.fills]
        if self.entry_net:
            o, n = self.entry_net
            out.append(f"  open_structures.entry_net {o:+} -> {n:+}")
        if self.close_net:
            o, n = self.close_net
            out.append(f"  open_structures.close_net {o:+} -> {n:+}")
        if self.realised:
            out.append(
                f"  realised {self.realised[0]:+.2f} -> {self.realised[1]:+.2f} "
                f"(delta {self.delta:+.2f})"
            )
        if self.tax_lot:
            lid, o, n = self.tax_lot
            out.append(f"  tax_lots {lid} realized_pnl {o:+.2f} -> {n:+.2f}")
        if self.outcome:
            out.append(f"  outcomes realised_pnl {self.outcome[0]} -> {self.outcome[1]}")
        out.append(
            "  decisions + reconcile:fill_sign_corrected"
            + (f" {self.decision_id}" if self.decision_id else "")
        )
        return out


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def find_sign_mismatches(conn: sqlite3.Connection) -> list[FillSignFix]:
    """Every filled execution whose fill sign disagrees with its one-signed band."""
    out: list[FillSignFix] = []
    for r in conn.execute(
        """SELECT proposal_hash, kind, structure_id, band_lo, band_hi, filled_qty, fill_price
           FROM executions
           WHERE filled_qty > 0 AND fill_price IS NOT NULL AND structure_id IS NOT NULL
           ORDER BY started_at, rowid"""
    ):
        try:
            lo, hi, px = Decimal(r[3]), Decimal(r[4]), Decimal(r[6])
        except (ArithmeticError, TypeError, ValueError):
            continue
        band = _sign(lo)
        if band == 0 or _sign(hi) != band or _sign(px) in (0, band):
            continue
        out.append(FillSignFix(str(r[0]), str(r[1]), str(r[2]), lo, hi, int(r[5]), px))
    return out


def _realised(conn: sqlite3.Connection, row: dict[str, Any]) -> Decimal | None:
    """The ladder's realised P&L of a closed structure: sum over its close tranches."""
    if row["status"] != "closed" or row["close_net"] is None:
        return None
    tranches, _ = _tranches(conn, row, _final_close_hash(conn, row))
    entry = Decimal(row["entry_net"])
    return sum((-(entry + px) * _HUNDRED * q for q, px in tranches), start=Decimal(0))


def _pnl_lot(conn: sqlite3.Connection, open_hash: str) -> tuple[str, Decimal] | None:
    """The closed tax lot carrying the structure's realised P&L (the first non-zero)."""
    rows = conn.execute(
        """SELECT l.id, l.realized_pnl FROM tax_lots l JOIN orders o ON o.id = l.order_id
           WHERE o.proposal_hash = ? AND l.closed_at IS NOT NULL ORDER BY l.rowid""",
        (open_hash,),
    ).fetchall()
    if not rows:
        return None
    pick = next((r for r in rows if r[1] not in (None, "") and Decimal(r[1]) != 0), rows[0])
    return str(pick[0]), Decimal(pick[1] or 0)


def _repair_one(
    conn: sqlite3.Connection,
    sid: str,
    fixes: list[FillSignFix],
    *,
    now: _dt.datetime,
    dry_run: bool,
) -> StructureRepair | None:
    raw = conn.execute("SELECT * FROM open_structures WHERE id = ?", (sid,)).fetchone()
    if raw is None:
        log.warning("journal.fill_sign_skipped", structure_id=sid, reason="unknown structure")
        return None
    row = dict(raw)
    rep = StructureRepair(sid, str(row["ticker"]), str(row["status"]), fixes)
    before = _realised(conn, row)
    lot = _pnl_lot(conn, row["open_proposal_hash"])
    old_outcome = conn.execute(
        """SELECT realised_pnl FROM outcomes WHERE proposal_hash = ?
           ORDER BY at DESC, rowid DESC LIMIT 1""",
        (row["open_proposal_hash"],),
    ).fetchone()
    for f in fixes:
        conn.execute(
            "UPDATE executions SET fill_price = ? WHERE proposal_hash = ? AND kind = ?",
            (str(f.new_fill), f.proposal_hash, f.kind),
        )
        band = _sign(f.band_lo)
        for fid, price in conn.execute(
            """SELECT f.id, f.price FROM fills f JOIN orders o ON o.id = f.order_id
               WHERE o.proposal_hash = ? ORDER BY f.rowid""",
            (f.proposal_hash,),
        ).fetchall():
            old = Decimal(price)
            if _sign(old) not in (0, band):
                new = Decimal(0) - old
                conn.execute("UPDATE fills SET price = ? WHERE id = ?", (str(new), fid))
                rep.fills.append((str(fid), old, new))
        if f.kind == "open" and Decimal(row["entry_net"]) == f.old_fill:
            rep.entry_net = (f.old_fill, f.new_fill)
            row["entry_net"] = str(f.new_fill)
            conn.execute(
                "UPDATE open_structures SET entry_net = ? WHERE id = ?", (str(f.new_fill), sid)
            )
        if (
            f.kind == "close"
            and row["close_net"] is not None
            and Decimal(row["close_net"]) == f.old_fill
            and row["exit_proposal_hash"] in (f.proposal_hash, None)
        ):
            rep.close_net = (f.old_fill, f.new_fill)
            row["close_net"] = str(f.new_fill)
            conn.execute(
                "UPDATE open_structures SET close_net = ? WHERE id = ?", (str(f.new_fill), sid)
            )
    after = _realised(conn, row)
    if before is not None and after is not None:
        rep.realised = (before, after)
        if lot is not None and after != before:
            new_lot = lot[1] + rep.delta
            rep.tax_lot = (lot[0], lot[1], new_lot)
            conn.execute(
                "UPDATE tax_lots SET realized_pnl = ? WHERE id = ?", (f"{new_lot:.2f}", lot[0])
            )
    if row["status"] == "closed":
        if not dry_run:
            rep.outcome_id = record_close_outcome(conn, sid, restate=True)
        new_outcome = conn.execute(
            """SELECT realised_pnl FROM outcomes WHERE proposal_hash = ?
               ORDER BY at DESC, rowid DESC LIMIT 1""",
            (row["open_proposal_hash"],),
        ).fetchone()
        rep.outcome = (
            None if old_outcome is None else old_outcome[0],
            None if dry_run or new_outcome is None else new_outcome[0],
        )
        if dry_run and rep.realised is not None:
            rep.outcome = (rep.outcome[0], f"{rep.realised[1]:.2f}")
    phash = next((f.proposal_hash for f in fixes if f.kind == "close"), fixes[0].proposal_hash)
    payload: dict[str, Any] = {
        "structure_id": sid,
        "executions": [
            {
                "proposal_hash": f.proposal_hash,
                "kind": f.kind,
                "old_fill": str(f.old_fill),
                "new_fill": str(f.new_fill),
            }
            for f in fixes
        ],
    }
    if rep.realised is not None:
        payload |= {
            "old_realized_pnl": f"{rep.realised[0]:.2f}",
            "new_realized_pnl": f"{rep.realised[1]:.2f}",
            "realized_pnl_delta": f"{rep.delta:.2f}",
        }
    if not dry_run:
        rec = JournalStore(conn).record(
            persona=JournalPersona.BROKER,
            stage=Stage.RECONCILE,
            subject=rep.ticker,
            choice=Choice.NOTED,
            reason_code=ReasonCode.RECONCILE_FILL_SIGN,
            reason_text=(
                "fill sign corrected: "
                + "; ".join(f"{f.kind} {f.old_fill:+} -> {f.new_fill:+}" for f in fixes)
                + (
                    f"; realised {rep.realised[0]:+.2f} -> {rep.realised[1]:+.2f}"
                    if rep.realised
                    else ""
                )
            ),
            proposal_hash=phash,
            payload=payload,
            at=now,
        )
        rep.decision_id = rec.id
    return rep


def repair_fill_signs(
    conn: sqlite3.Connection, *, now: _dt.datetime, dry_run: bool = False
) -> list[StructureRepair]:
    """Restate every structure with a sign-mismatched fill; one transaction each.

    ``dry_run`` computes every change inside the transaction and rolls it back.
    """
    by_sid: dict[str, list[FillSignFix]] = defaultdict(list)
    for f in find_sign_mismatches(conn):
        by_sid[f.structure_id].append(f)
    out: list[StructureRepair] = []
    if conn.in_transaction:
        conn.commit()
    for sid, fixes in by_sid.items():
        conn.execute("BEGIN")
        try:
            rep = _repair_one(conn, sid, fixes, now=now, dry_run=dry_run)
        except Exception:
            conn.rollback()
            raise
        if dry_run or rep is None:
            conn.rollback()
        else:
            conn.commit()
            log.info(
                "journal.fill_sign_corrected",
                structure_id=sid,
                delta=str(rep.delta),
                decision_id=rep.decision_id,
            )
        if rep is not None:
            out.append(rep)
    return out


@dataclass(frozen=True)
class SnapshotRestatement:
    """One production ``pnl_snapshots`` row whose realised P&L is restated."""

    snapshot_id: str
    day: str
    old_realized: Decimal
    new_realized: Decimal
    old_total: Decimal
    new_total: Decimal

    def line(self) -> str:
        return (
            f"pnl_snapshots {self.snapshot_id} ({self.day}) realized "
            f"{self.old_realized:+.2f} -> {self.new_realized:+.2f}, total "
            f"{self.old_total:+.2f} -> {self.new_total:+.2f}"
        )


def _ts(raw: str | None) -> _dt.datetime | None:
    import datetime as dt

    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)


def _et_day(ts: _dt.datetime) -> str:
    from arc.utils.calendar import ET

    return ts.astimezone(ET).date().isoformat()


def _corrections(conn: sqlite3.Connection) -> list[tuple[_dt.datetime, Decimal]]:
    """(P&L lot's close time, realised delta) of every ``fill_sign_corrected`` decision."""
    import json

    out: list[tuple[_dt.datetime, Decimal]] = []
    for (payload,) in conn.execute(
        "SELECT payload FROM decisions WHERE reason_code = ? ORDER BY at, rowid",
        (str(ReasonCode.RECONCILE_FILL_SIGN),),
    ).fetchall():
        p = json.loads(payload or "{}")
        sid, delta = p.get("structure_id"), p.get("realized_pnl_delta")
        if not sid or delta in (None, ""):
            continue
        srow = conn.execute(
            "SELECT open_proposal_hash FROM open_structures WHERE id = ?", (sid,)
        ).fetchone()
        if srow is None:
            continue
        lot = conn.execute(
            """SELECT l.closed_at FROM tax_lots l JOIN orders o ON o.id = l.order_id
               WHERE o.proposal_hash = ? AND l.closed_at IS NOT NULL
                 AND l.realized_pnl IS NOT NULL AND l.realized_pnl != ''
                 AND CAST(l.realized_pnl AS REAL) != 0
               ORDER BY l.rowid LIMIT 1""",
            (srow[0],),
        ).fetchone()
        closed = _ts(lot[0]) if lot is not None else None
        if closed is not None:
            out.append((closed, Decimal(str(delta))))
    return out


def restate_pnl_snapshots(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> list[SnapshotRestatement]:
    """Re-derive production day snapshots on days a corrected lot closed (one transaction)."""
    import json

    corrections = _corrections(conn)
    days = {_et_day(c) for c, _ in corrections}
    if not days:
        return []
    lots = [
        (ts, Decimal(str(r[1] or 0)))
        for r in conn.execute(
            "SELECT closed_at, realized_pnl FROM tax_lots WHERE closed_at IS NOT NULL"
        ).fetchall()
        if (ts := _ts(r[0])) is not None
    ]
    out: list[SnapshotRestatement] = []
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        for r in conn.execute(
            """SELECT id, snapshot_at, realized, unrealized, total, details_json
               FROM pnl_snapshots WHERE arm_id IS NULL ORDER BY snapshot_at, rowid"""
        ).fetchall():
            day = json.loads(r[5] or "{}").get("day")
            at = _ts(r[1])
            if day not in days or at is None:
                continue
            new = sum((v for ts, v in lots if _et_day(ts) == day and ts <= at), start=Decimal(0))
            old = Decimal(str(r[2]))
            expected = sum(
                (d for c, d in corrections if _et_day(c) == day and c <= at), start=Decimal(0)
            )
            gap = new - old
            if gap == 0:
                continue
            if gap != expected:
                log.warning(
                    "journal.fill_sign_snapshot_skipped",
                    snapshot_id=r[0],
                    day=day,
                    stored=str(old),
                    recomputed=str(new),
                    corrections=str(expected),
                )
                continue
            unreal = Decimal(str(r[3]))
            rest = SnapshotRestatement(str(r[0]), day, old, new, Decimal(str(r[4])), new + unreal)
            conn.execute(
                "UPDATE pnl_snapshots SET realized = ?, total = ? WHERE id = ?",
                (f"{rest.new_realized:.4f}", f"{rest.new_total:.4f}", rest.snapshot_id),
            )
            out.append(rest)
    except Exception:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
        for s in out:
            log.info(
                "journal.fill_sign_snapshot_restated",
                snapshot_id=s.snapshot_id,
                day=s.day,
                old=str(s.old_realized),
                new=str(s.new_realized),
            )
    return out
