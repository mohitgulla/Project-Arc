"""``arc experiment repair-ledger``: net out double-booked arm fills (E10.2c).

Before E10.2c, :func:`arc.experiments.virtual.record_fills` keyed a fill row on
``order:symbol:filled_at:qty:price``. Alpaca's mleg leg ``filled_at`` drifts by
microseconds between calls, so a re-fetched fill booked a second time and the
arm's virtual cash (hence its equity, the evaluator's ``virtual_equity``) was off
by that fill's premium (XP-10: -$690 net).

The repair, per arm store, in one transaction:

1. **Ledger.** For every ``(order, symbol)`` whose rows book more contracts than
   the broker's cumulative fill (the largest ``|qty|`` one legacy row booked, or
   the ``cum_qty`` an E10.2c row recorded), append one ``adjust`` row netting the
   excess out at the average booked price, dated like the last duplicate (same
   ET day, same settlement day). The ledger is append-only: nothing is deleted or
   rewritten, and a second run finds nothing to adjust.
2. **Snapshots.** Every EOD ``pnl_snapshots`` row carrying ``virtual_equity`` is
   restated: virtual cash replayed from the corrected ledger as of that day, plus
   the marks of the broker positions in the same reconcile's
   ``positions_snapshots`` row. That is exact whatever the duplicate's booking
   time was (a duplicate booked after a day's EOD reconcile did not move that
   day's snapshot, and the restatement leaves it where it belongs). ``equity`` /
   ``virtual_equity`` / ``cash`` move by the same delta; ``last_equity``, an
   ``arc_close`` ``prev_close`` and ``day_pnl`` follow. The old values are kept in
   ``details_json.restated``.
3. **Stored day series.** The CLI then appends a fresh ``experiment_reports`` row
   to control (append-only), computed on the restated snapshots.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from arc.experiments.virtual import LedgerRow, append_row, order_symbol, replay, rows
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "LedgerAdjustment",
    "LedgerRepair",
    "SnapshotRestatement",
    "duplicate_adjustments",
    "repair_ledger",
]

log = structlog.get_logger(__name__)

CENT = Decimal("0.01")


@dataclass(frozen=True)
class LedgerAdjustment:
    """One ``adjust`` row netting out the excess rows of ``(order, symbol)``."""

    order: str
    symbol: str
    row_ids: tuple[int, ...]
    qty: Decimal
    amount: Decimal
    settles_on: _dt.date | None
    at: _dt.datetime

    @property
    def ref(self) -> str:
        return f"{self.order}:{self.symbol}:adjust:{self.row_ids[-1]}"

    def row(self, arm_id: str) -> LedgerRow:
        return LedgerRow(
            arm_id=arm_id,
            kind="adjust",
            ref=self.ref,
            amount=self.amount,
            settles_on=self.settles_on,
            at=self.at,
            detail={
                "symbol": self.symbol,
                "qty": str(self.qty),
                "reason": "duplicate_fill",
                "ledger_ids": list(self.row_ids),
                "card": "E10.2c",
            },
        )

    def line(self) -> str:
        ids = "/".join(str(i) for i in self.row_ids)
        return (
            f"adjust {self.symbol} order {self.order[:8]} (ledger ids {ids}): "
            f"qty {self.qty:+} amount {self.amount:+,.2f}"
        )


@dataclass(frozen=True)
class SnapshotRestatement:
    snapshot_id: str
    day: str
    old_equity: Decimal
    new_equity: Decimal
    old_day_pnl: Decimal | None
    new_day_pnl: Decimal | None

    def line(self) -> str:
        def pnl(v: Decimal | None) -> str:
            return "-" if v is None else f"{v:+,.2f}"

        return (
            f"pnl_snapshots {self.snapshot_id} ({self.day}) virtual equity "
            f"{self.old_equity:,.2f} -> {self.new_equity:,.2f}, day P&L "
            f"{pnl(self.old_day_pnl)} -> {pnl(self.new_day_pnl)}"
        )


@dataclass
class LedgerRepair:
    arm_id: str
    dry_run: bool
    fill_sum_before: Decimal
    fill_sum_after: Decimal
    adjustments: list[LedgerAdjustment] = field(default_factory=list)
    snapshots: list[SnapshotRestatement] = field(default_factory=list)

    def lines(self) -> list[str]:
        verb = "would append" if self.dry_run else "appended"
        return [
            *(a.line() for a in self.adjustments),
            *(s.line() for s in self.snapshots),
            f"{'dry run: ' if self.dry_run else ''}{self.arm_id}: ledger fill sum "
            f"{self.fill_sum_before:+,.2f} -> {self.fill_sum_after:+,.2f}; {verb} "
            f"{len(self.adjustments)} adjust row(s), {len(self.snapshots)} pnl snapshot(s) "
            "restated",
        ]


def _numbered(conn: sqlite3.Connection, arm_id: str) -> list[tuple[int, LedgerRow]]:
    ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM virtual_ledger WHERE arm_id = ? ORDER BY id", (arm_id,)
        )
    ]
    return list(zip(ids, rows(conn, arm_id), strict=True))


def _qty(r: LedgerRow, key: str = "qty") -> Decimal:
    return Decimal(str(r.detail.get(key, r.detail.get("qty", "0"))))


def duplicate_adjustments(ledger: list[tuple[int, LedgerRow]]) -> list[LedgerAdjustment]:
    """The ``adjust`` rows that net each ``(order, symbol)`` down to its broker fill. Pure."""
    groups: dict[tuple[str, str], list[tuple[int, LedgerRow]]] = {}
    for rid, r in ledger:
        if r.kind in ("fill", "adjust"):
            groups.setdefault(order_symbol(r.ref), []).append((rid, r))
    out: list[LedgerAdjustment] = []
    for (order, symbol), grp in sorted(groups.items()):
        fills = [(rid, r) for rid, r in grp if r.kind == "fill"]
        if not fills:
            continue
        booked_qty = sum((_qty(r) for _, r in grp), Decimal(0))
        cum = max(abs(_qty(r, "cum_qty")) for _, r in fills)
        if abs(booked_qty) <= cum:
            continue
        booked_cash = sum((r.amount for _, r in grp), Decimal(0))
        sign = Decimal(1) if booked_qty > 0 else Decimal(-1)
        excess = booked_qty - sign * cum
        last = fills[-1][1]
        out.append(
            LedgerAdjustment(
                order=order,
                symbol=symbol,
                row_ids=tuple(rid for rid, _ in fills),
                qty=-excess,
                amount=-(booked_cash * excess / booked_qty).quantize(CENT),
                settles_on=last.settles_on,
                at=last.at,
            )
        )
    return out


def _dec(v: object) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def _broker_marks(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """``snapshot_at`` -> signed market value of the option positions that reconcile saw."""
    out: dict[str, Decimal] = {}
    for at, pj in conn.execute(
        "SELECT snapshot_at, positions_json FROM positions_snapshots ORDER BY snapshot_at, rowid"
    ):
        total = Decimal(0)
        for b in json.loads(pj or "{}").get("broker", []):
            mv = _dec(b.get("market_value"))
            if mv is None or b.get("asset_class", "us_option") != "us_option":
                continue
            total += -abs(mv) if b.get("side") == "short" else abs(mv)
        out[str(at)] = total
    return out


def _shift(d: dict[str, object], key: str, delta: Decimal) -> None:
    v = _dec(d.get(key))
    if v is not None:
        d[key] = str(v + delta)


def _restate_snapshots(
    conn: sqlite3.Connection, ledger: list[LedgerRow], added: list[LedgerAdjustment]
) -> list[tuple[SnapshotRestatement, str]]:
    """Each restated EOD snapshot with its new ``details_json`` (nothing written)."""
    marks = _broker_marks(conn)
    out: list[tuple[SnapshotRestatement, str]] = []
    prev: tuple[Decimal, Decimal] | None = None  # (old, new) virtual equity of the prior EOD
    for sid, at, raw in conn.execute(
        """SELECT id, snapshot_at, details_json FROM pnl_snapshots
           WHERE json_extract(details_json, '$.day') IS NOT NULL
             AND json_extract(details_json, '$.virtual_equity') IS NOT NULL
           ORDER BY snapshot_at, rowid"""
    ).fetchall():
        d = json.loads(raw)
        day = _dt.date.fromisoformat(d["day"])
        old_eq = Decimal(str(d["virtual_equity"]))
        mark = marks.get(str(at))
        if mark is None:
            log.warning("experiments.repair_snapshot_skipped", snapshot_id=sid, reason="no marks")
            prev = None
            continue
        new_eq = replay(ledger, as_of=day).cash + mark
        new: dict[str, object] = dict(d)
        new["virtual_equity"] = str(new_eq)
        _shift(new, "equity", new_eq - old_eq)
        _shift(new, "cash", new_eq - old_eq)
        # start-of-day: every duplicate of an earlier day was booked by this EOD
        # (each sync re-fetches all fills since t0)
        sod = sum((a.amount for a in added if a.at.astimezone(ET).date() < day), Decimal(0))
        _shift(new, "last_equity", sod)
        prev_close = _dec(d.get("prev_close"))
        if prev_close is not None:
            if d.get("prev_close_source") == "arc_close":
                if prev is not None and prev[0] == prev_close:
                    prev_close = prev[1]
            else:  # broker_last_equity: the (virtual) start-of-day equity
                prev_close += sod
            new["prev_close"] = str(prev_close)
        old_pnl = _dec(d.get("day_pnl"))
        new_pnl = old_pnl if prev_close is None or old_pnl is None else new_eq - prev_close
        if new_pnl is not None:
            new["day_pnl"] = str(new_pnl)
        prev = (old_eq, new_eq)
        if new == d:
            continue
        new["restated"] = {
            "by": "E10.2c repair-ledger",
            **{k: d.get(k) for k in ("virtual_equity", "equity", "cash", "last_equity",
                                     "prev_close", "day_pnl")},
        }  # fmt: skip
        out.append(
            (
                SnapshotRestatement(str(sid), day.isoformat(), old_eq, new_eq, old_pnl, new_pnl),
                json.dumps(new, default=str),
            )
        )
    return out


def repair_ledger(conn: sqlite3.Connection, arm_id: str, *, dry_run: bool = False) -> LedgerRepair:
    """Append the ``adjust`` rows and restate the arm's EOD snapshots (one transaction).

    ``dry_run`` computes the same repair in memory and writes nothing, so it runs
    on a read-only connection (and on a store not yet migrated to ``adjust``).
    """
    before = _numbered(conn, arm_id)
    fill_sum = sum((r.amount for _, r in before if r.kind in ("fill", "adjust")), Decimal(0))
    rep = LedgerRepair(arm_id, dry_run, fill_sum, fill_sum)
    adjustments = duplicate_adjustments(before)
    if not adjustments:
        return rep
    ledger = [r for _, r in before] + [a.row(arm_id) for a in adjustments]
    rep.adjustments = adjustments
    rep.fill_sum_after = sum((r.amount for r in ledger if r.kind in ("fill", "adjust")), Decimal(0))
    restated = _restate_snapshots(conn, ledger, adjustments)
    rep.snapshots = [s for s, _ in restated]
    if dry_run:
        return rep
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        for a in adjustments:
            if not append_row(conn, a.row(arm_id)):  # pragma: no cover - a race with a 2nd run
                msg = f"adjust row {a.ref} already booked; re-run the repair"
                raise RuntimeError(msg)
        for s, details in restated:
            conn.execute(
                "UPDATE pnl_snapshots SET details_json = ? WHERE id = ?", (details, s.snapshot_id)
            )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    log.info(
        "experiments.ledger_repaired",
        arm_id=arm_id,
        adjustments=len(rep.adjustments),
        snapshots=len(rep.snapshots),
        fill_sum=str(rep.fill_sum_after),
    )
    return rep
