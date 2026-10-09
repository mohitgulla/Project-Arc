"""Fills and the local position model, shared by the ladder and the re-attach (E6.2, E11.2).

:func:`fill_net_price` turns a broker status into the signed per-unit net price;
:func:`record_fill` writes the ``fills`` row (once per broker order: a duplicate
``(order_id, broker_fill_id)`` is the D72 unique index's no-op); :func:`apply_fill`
updates ``open_structures`` / ``tax_lots`` / the exit journal exactly as the
D24 ladder does after a fill. :mod:`arc.execution.reattach` calls the same
functions for an execution whose ladder process died, so an adopted fill lands in
the position model by the same code path.

Deterministic: DB only, no broker, no network, no LLM.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.ttl import to_db
from arc.journal.outcomes import record_close_outcome
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.store.execution import OpenStructureRepo
from arc.store.repos import FillRepo, TaxLotRepo

if TYPE_CHECKING:
    from arc.broker.base import BrokerOrderStatus
    from arc.models import Structure

__all__ = ["apply_fill", "fill_net_price", "record_fill"]

log = structlog.get_logger(__name__)

_HUNDRED = Decimal(100)


def fill_net_price(status: BrokerOrderStatus, fallback: Decimal) -> Decimal:
    """Per-unit net fill price (+ debit / − credit).

    From the legs' fill prices when every leg reports one (``buy`` +, ``sell`` −,
    weighted by leg qty / order qty); else the order's own average, which the
    broker reports *unsigned*: ``+avg`` for a ``buy``, ``−avg`` for a ``sell``,
    and the sign of ``fallback`` (the attempt's signed limit) when the side is
    unknown (E6.2f); else ``fallback``.

    Guard (deterministic): a limit order never fills worse than its limit, so a
    credit limit (< 0) can't fill as a debit, and a one-leg order is always a
    pure debit (buy) or credit (sell). A fill whose sign disagrees in those cases
    is logged ``fill_sign_mismatch`` and takes the limit's sign. A debit-limit
    mleg filled as a credit is genuine price improvement and is kept.
    """
    legs = status.legs or []
    qty = status.filled_qty
    if legs and qty > 0 and all(leg.get("filled_avg_price") for leg in legs):
        net = Decimal(0)
        for leg in legs:
            sign = 1 if str(leg.get("side")) == "buy" else -1
            ratio = Decimal(str(leg.get("filled_qty") or leg.get("qty") or 0)) / qty
            net += sign * ratio * Decimal(str(leg["filled_avg_price"]))
        return _sign_guard(status, net.quantize(Decimal("0.0001")), fallback, single=False)
    if status.filled_avg_price is not None:
        avg = abs(status.filled_avg_price)
        side = (status.side or "").lower()
        if side not in ("buy", "sell"):  # unknown: the signed limit decides
            side = "sell" if fallback < 0 else "buy"
            if fallback == 0:
                log.warning(
                    "fill_sign_unknown",
                    broker_order_id=status.broker_order_id,
                    filled_avg_price=str(avg),
                )
        price = avg if side == "buy" else -avg
        return _sign_guard(status, price, fallback, single=True)
    return fallback


def _sign_guard(
    status: BrokerOrderStatus, price: Decimal, limit: Decimal, *, single: bool
) -> Decimal:
    """Flip *price* to the limit's sign when the two can't legitimately disagree."""
    credit_as_debit = limit < 0 < price
    debit_as_credit = single and price < 0 < limit
    if not (credit_as_debit or debit_as_credit):
        return price
    log.warning(
        "fill_sign_mismatch",
        broker_order_id=status.broker_order_id,
        fill=str(price),
        limit=str(limit),
        side=status.side,
    )
    return -price


def record_fill(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    status: BrokerOrderStatus,
    limit_price: Decimal,
    now: _dt.datetime,
    run_id: str | None,
) -> tuple[int, Decimal | None]:
    """Write the ``fills`` row for *status*; ``(qty, net price)``, ``(0, None)`` if unfilled.

    D72: a second write of the same ``(order_id, broker_fill_id)`` (a ladder and
    a re-attach racing) hits the unique index and is a logged no-op; the row
    already stored decides the returned price.
    """
    qty = int(status.filled_qty)
    if qty <= 0:
        return 0, None
    price = fill_net_price(status, limit_price)
    try:
        FillRepo(conn).insert(
            order_id=order_id,
            qty=qty,
            price=str(price),
            filled_at=to_db(status.updated_at or now),
            broker_fill_id=status.broker_order_id,
            run_id=run_id,
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT qty, price FROM fills WHERE order_id = ? AND broker_fill_id = ?",
            (order_id, status.broker_order_id),
        ).fetchone()
        log.warning(
            "execution.fill_already_recorded",
            order_id=order_id,
            broker_order_id=status.broker_order_id,
        )
        if row is not None:
            return int(row[0]), Decimal(str(row[1]))
    return qty, price


def apply_fill(
    conn: sqlite3.Connection,
    *,
    kind: str,
    phash: str,
    ticker: str,
    candidate_id: str,
    structure: Structure,
    order_id: str,
    filled_qty: int,
    fill_price: Decimal,
    structure_id: str | None,
    now: _dt.datetime,
    run_id: str | None,
) -> str:
    """Book a fill in the position model; returns the ``open_structures`` id.

    ``open``: a new structure plus one tax lot per leg (on *order_id*, the filling
    attempt). ``close``: reduce *structure_id*; a full close records the outcome,
    closes the lots and journals ``exit:closed`` with the realised P&L.
    """
    if kind == "open":
        sid = OpenStructureRepo(conn).open(
            ticker=ticker,
            open_proposal_hash=phash,
            candidate_id=candidate_id,
            structure_json=structure.model_dump_json(),
            contracts=filled_qty,
            entry_net=fill_price,
            now=now,
        )
        lots = TaxLotRepo(conn)
        for leg in structure.legs:
            lots.open_lot(
                order_id=order_id,
                ticker=ticker,
                occ_symbol=leg.occ_symbol,
                side=str(leg.side),
                qty=leg.ratio * filled_qty,
                open_price=str(leg.premium if leg.premium is not None else ""),
                opened_at=to_db(now),
                run_id=run_id,
            )
        return sid

    if structure_id is None:
        msg = "a close execution needs the structure it closes"
        raise ValueError(msg)
    repo = OpenStructureRepo(conn)
    row = repo.get(structure_id)
    closed = repo.reduce(
        structure_id, closed_qty=filled_qty, close_net=fill_price, now=now, commit=False
    )
    if closed:  # the outcome commits with the close (E7.4b)
        record_close_outcome(conn, structure_id, expired=False)
    conn.commit()
    if row is not None:
        pnl = -(Decimal(row["entry_net"]) + fill_price) * _HUNDRED * filled_qty
        text = f"closed {filled_qty} @ {fill_price:+}; realized {pnl:+.2f}"
        if closed:
            _close_lots(conn, row, pnl, now)
        with conn:
            JournalStore(conn).record(
                persona=JournalPersona.BROKER,
                stage=Stage.EXIT,
                subject=ticker,
                choice=Choice.FILLED,
                reason_code=ReasonCode.EXIT_CLOSED,
                reason_text=text,
                proposal_hash=phash,
                payload={"structure_id": structure_id, "realized_pnl": str(pnl)},
                at=now,
                run_id=run_id,
            )
    return structure_id


def _close_lots(
    conn: sqlite3.Connection, row: dict[str, Any], pnl: Decimal, now: _dt.datetime
) -> None:
    """Close the structure's open lots; the realised P&L sits on the first lot (wash sale)."""
    lots = conn.execute(
        """SELECT l.id FROM tax_lots l JOIN orders o ON o.id = l.order_id
           WHERE o.proposal_hash = ? AND l.closed_at IS NULL ORDER BY l.rowid""",
        (row["open_proposal_hash"],),
    ).fetchall()
    repo = TaxLotRepo(conn)
    for i, lot in enumerate(lots):
        repo.close_lot(
            lot["id"] if isinstance(lot, sqlite3.Row) else lot[0],
            close_price="",
            realized_pnl=str(pnl if i == 0 else Decimal(0)),
            closed_at=now.astimezone(_dt.UTC).isoformat(),
        )
