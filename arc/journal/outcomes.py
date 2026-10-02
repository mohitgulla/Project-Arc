"""Write ``outcomes`` rows when a traded structure closes or expires (E7.4b).

The production writer of :meth:`arc.journal.store.JournalStore.record_outcome`.
:func:`record_close_outcome` is called in the same transaction as the close
(``arc.execution.ladder`` on a fill that fully closes a structure,
``arc.reconcile.engine`` when it settles an expired one), and by
``arc journal backfill-outcomes`` for structures closed before this writer existed.

Scope: closes and expiries only. ``not_traded`` / ``never_filled`` proposals are
not written here: the journal views already derive them from ``proposals`` +
``approval_requests`` + ``executions`` (``arc.journal.report._not_traded``), so a
row per rejected card would only duplicate that.

Prices. ``open_structures.entry_net`` and every close fill use the order sign
(+ paid / - received). :func:`arc.journal.attribution.attribute` wants the exit
as the *value of the position* in the entry convention, which is the negated
close price: a debit spread bought at +4.60 and sold for a 4.50 credit
(close fill -4.50) has exit value +4.50 and P&L (4.50 - 4.60) x 100 x n.

Partial closes. One outcome is written, when the structure is fully closed, for
the **original** size: the sum of every close tranche. The exit fill is the
quantity-weighted mean of the tranche exit values, which makes
``(exit - entry) x 100 x contracts`` equal the sum of the per-tranche P&L the
ladder books. Tranches follow the tower's rule (``arc.tower.data_trades``): every
filled close execution of the structure, plus the opened quantity (the open
execution's ``filled_qty``) not covered by them at the structure's ``close_net``.
That remainder is the final ladder fill when called from the ladder (its
execution row is not finished yet) or the reconcile expiry settlement. Without
an open execution on record, the structure's remaining ``contracts`` at
``close_net`` is the final tranche.

Idempotency. One closed outcome per ``open_proposal_hash``: when the latest row
for it is already ``closed`` / ``expired_worthless`` nothing is written. A latest
row in any other status (e.g. ``open``) is superseded via ``supersedes_id``.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.ttl import from_db
from arc.gate.rules import proposal_hash as hash_proposal
from arc.journal.attribution import attribute
from arc.journal.models import OutcomeRecord, OutcomeStatus
from arc.journal.store import JournalStore
from arc.models import Proposal, QuantMetrics, Sizing, Structure
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "BackfillResult",
    "backfill_outcomes",
    "build_close_outcome",
    "load_proposal",
    "record_close_outcome",
]

log = structlog.get_logger(__name__)

_FINAL = (OutcomeStatus.CLOSED.value, OutcomeStatus.EXPIRED_WORTHLESS.value)


def load_proposal(conn: sqlite3.Connection, proposal_hash: str) -> Proposal:
    """The stored :class:`Proposal` for *proposal_hash*.

    The ``proposal`` context entry written by the same run is the full payload
    (it carries ``limit_price``); when it is missing or hashes differently the
    ``proposals`` row's JSON columns are used (limit = structure mid). Raises
    ``LookupError`` when the proposal is unknown.
    """
    row = conn.execute(
        """SELECT p.*,
                  (SELECT c.payload FROM context_entries c
                    WHERE c.kind = 'proposal' AND c.run_id = p.run_id AND c.subject = p.ticker
                    ORDER BY c.created_at DESC, c.rowid DESC LIMIT 1) AS context_payload
           FROM proposals p WHERE p.proposal_hash = ?""",
        (proposal_hash,),
    ).fetchone()
    if row is None:
        msg = f"no proposal {proposal_hash[:12]}"
        raise LookupError(msg)
    if row["context_payload"]:
        prop = Proposal.model_validate_json(row["context_payload"])
        if hash_proposal(prop) == proposal_hash:
            return prop
    return Proposal(
        candidate_id=row["candidate_id"],
        structure=Structure.model_validate_json(row["structure_json"]),
        thesis=row["thesis"] or "",
        quant=QuantMetrics.model_validate_json(row["quant_json"]),
        risk_narrative=row["risk_narrative"] or "",
        sizing=Sizing.model_validate_json(row["sizing_json"]),
        expires_at=_dt.datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")),
    )


def _final_close_hash(conn: sqlite3.Connection, row: dict[str, Any]) -> str | None:
    """The close execution that filled the final tranche, or ``None`` (expiry settlement).

    The ladder sets ``exit_proposal_hash`` before every close attempt and
    ``reduce`` clears it after a partial close, so the final tranche came from a
    close execution only when ``exit_proposal_hash`` names a filled one.
    """
    if row["exit_proposal_hash"] is None:
        return None
    hit = conn.execute(
        """SELECT 1 FROM executions WHERE proposal_hash = ? AND kind = 'close'
           AND filled_qty > 0""",
        (row["exit_proposal_hash"],),
    ).fetchone()
    return None if hit is None else str(row["exit_proposal_hash"])


def _tranches(
    conn: sqlite3.Connection, row: dict[str, Any], final_hash: str | None
) -> tuple[list[tuple[int, Decimal]], bool]:
    """``(qty, close price)`` per close tranche of a fully closed structure (module doc).

    The flag is True when the last tranche is a remainder no close execution
    accounts for (the reconcile expiry settlement, once the ladder has finished).
    """
    closes = [
        (int(e[0]), Decimal(e[1]), str(e[2]))
        for e in conn.execute(
            """SELECT filled_qty, fill_price, proposal_hash FROM executions
               WHERE structure_id = ? AND kind = 'close' AND filled_qty > 0
                 AND fill_price IS NOT NULL
               ORDER BY started_at, rowid""",
            (row["id"],),
        )
    ]
    opened = conn.execute(
        """SELECT filled_qty FROM executions
           WHERE proposal_hash = ? AND kind = 'open' AND filled_qty > 0""",
        (row["open_proposal_hash"],),
    ).fetchone()
    if opened is not None:
        # opened qty minus the finished close tranches; the rest closed at close_net
        out = [(q, px) for q, px, _ in closes]
        rest = int(opened[0]) - sum(q for q, _ in out)
        return ([*out, (rest, Decimal(row["close_net"]))], True) if rest > 0 else (out, False)
    # no open execution on record: the remaining contracts closed at close_net
    out = [(q, px) for q, px, h in closes if h != final_hash]
    return [*out, (int(row["contracts"]), Decimal(row["close_net"]))], final_hash is None


def build_close_outcome(
    conn: sqlite3.Connection,
    structure_id: str,
    *,
    expired: bool | None = None,
    settlement: Decimal | None = None,
    supersedes_id: str | None = None,
) -> OutcomeRecord:
    """The outcome for a fully closed ``open_structures`` row (no write).

    ``expired=None`` infers it: the final tranche is a remainder that no filled
    close execution accounts for (see :func:`_tranches`).
    Raises ``LookupError`` for an unknown structure or proposal and ``ValueError``
    when the structure is not closed.
    """
    raw = conn.execute("SELECT * FROM open_structures WHERE id = ?", (structure_id,)).fetchone()
    if raw is None:
        msg = f"unknown open structure {structure_id}"
        raise LookupError(msg)
    row = dict(raw)
    if row["status"] != "closed" or row["close_net"] is None or row["closed_at"] is None:
        msg = f"structure {structure_id} is not closed"
        raise ValueError(msg)
    final_hash = _final_close_hash(conn, row)
    tranches, settled = _tranches(conn, row, final_hash)
    if expired is None:
        expired = settled
    phash = row["open_proposal_hash"]
    proposal = load_proposal(conn, phash)
    n = sum(q for q, _ in tranches)
    total = sum((Decimal(q) * px for q, px in tranches), start=Decimal(0))
    exit_value = (Decimal(0) - total) / n  # 0 - x, not -x: no "-0" for a worthless expiry
    opened, closed = from_db(row["opened_at"]), from_db(row["closed_at"])
    return attribute(
        proposal,
        proposal_hash=phash,
        at=closed,
        traded=True,
        entry_fill=Decimal(row["entry_net"]),
        exit_fill=exit_value,
        opened_at=opened.astimezone(ET).date(),
        closed_at=closed.astimezone(ET).date(),
        exit_reason="expiry" if expired else row["exit_reason"],
        settlement=settlement,
        expired=expired,
        contracts=n,
        supersedes_id=supersedes_id,
    )


def record_close_outcome(
    conn: sqlite3.Connection,
    structure_id: str,
    *,
    expired: bool | None = None,
    settlement: Decimal | None = None,
) -> str | None:
    """Append the outcome of a fully closed structure; no commit (caller's transaction).

    Returns the new row id, or ``None`` when the proposal already has a closed
    outcome (idempotent) or the outcome cannot be built (logged: the close itself
    must never fail because of the journal).
    """
    store = JournalStore(conn)
    raw = conn.execute(
        "SELECT open_proposal_hash FROM open_structures WHERE id = ?", (structure_id,)
    ).fetchone()
    if raw is None:
        log.warning("journal.outcome_skipped", structure_id=structure_id, reason="unknown")
        return None
    latest = store.latest_outcome_row(raw[0])
    if latest is not None and latest[1] in _FINAL:
        return None
    try:
        rec = build_close_outcome(
            conn,
            structure_id,
            expired=expired,
            settlement=settlement,
            supersedes_id=latest[0] if latest else None,
        )
    except (LookupError, ValueError) as exc:
        log.warning("journal.outcome_skipped", structure_id=structure_id, reason=str(exc))
        return None
    oid = store.record_outcome(rec)
    log.info(
        "journal.outcome_recorded",
        structure_id=structure_id,
        proposal_hash=rec.proposal_hash,
        status=str(rec.status),
        realised_pnl=str(rec.realised_pnl),
    )
    return oid


@dataclass(frozen=True)
class BackfillResult:
    structure_id: str
    proposal_hash: str
    ticker: str
    action: str  # written | would_write | exists | skipped
    status: str | None = None
    exit_fill: str | None = None
    realised_pnl: str | None = None
    detail: str = ""


def backfill_outcomes(conn: sqlite3.Connection, *, dry_run: bool = False) -> list[BackfillResult]:
    """Write the missing closed outcome of every closed structure (idempotent).

    Expiry is inferred per structure (see :func:`build_close_outcome`); the D19
    shadow stays empty (no settlement price is fetched). ``dry_run`` builds the
    records without writing. Everything is written in one transaction.
    """
    rows = conn.execute(
        """SELECT id, ticker, open_proposal_hash FROM open_structures
           WHERE status = 'closed' ORDER BY closed_at, id"""
    ).fetchall()
    store = JournalStore(conn)
    out: list[BackfillResult] = []
    with conn:
        for r in rows:
            sid, ticker, phash = str(r[0]), str(r[1]), str(r[2])
            latest = store.latest_outcome_row(phash)
            if latest is not None and latest[1] in _FINAL:
                out.append(BackfillResult(sid, phash, ticker, "exists", latest[1]))
                continue
            try:
                rec = build_close_outcome(conn, sid, supersedes_id=latest[0] if latest else None)
            except (LookupError, ValueError) as exc:
                out.append(BackfillResult(sid, phash, ticker, "skipped", detail=str(exc)))
                continue
            if not dry_run:
                store.record_outcome(rec)
            out.append(
                BackfillResult(
                    sid,
                    phash,
                    ticker,
                    "would_write" if dry_run else "written",
                    str(rec.status),
                    None if rec.exit_fill is None else str(rec.exit_fill),
                    None if rec.realised_pnl is None else str(rec.realised_pnl),
                )
            )
    return out
