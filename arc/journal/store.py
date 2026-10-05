"""Append-only storage of the decision journal (E7.4).

Write methods **do not commit**. Pipeline steps call them before the write
that commits their existing output (a context entry, a proposal row, an
approval), so the decision rows and that output land in one transaction; a
failure rolls both back. :meth:`JournalStore.add_review` is the exception: a
review stands alone, so it commits its own transaction.

The tables reject UPDATE and DELETE (triggers, 009_journal.sql). A correction
is a new row whose ``supersedes_id`` names the row it corrects.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from arc.context.ttl import from_db, to_db
from arc.journal import legacy
from arc.journal.models import (
    DecisionRecord,
    DecisionReview,
    MarketContext,
    OutcomeRecord,
)
from arc.journal.reasons import STAGE_ORDER, Choice, JournalPersona, ReasonCode, Stage

if TYPE_CHECKING:
    import datetime as _dt

__all__ = ["JournalStore", "Recorder", "ReviewCitationError"]


class ReviewCitationError(ValueError):
    """A review cites a decision (or targets a proposal) that is not in the journal."""


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _dec(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


class JournalStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- decisions -----------------------------------------------------------

    def record(
        self,
        *,
        persona: JournalPersona | str,
        stage: Stage | str,
        subject: str,
        choice: Choice | str,
        reason_code: ReasonCode | str,
        at: _dt.datetime,
        reason_text: str = "",
        confidence: float | None = None,
        chain_run_id: str | None = None,
        run_id: str | None = None,
        inputs_snapshot_id: str | None = None,
        persona_call_id: str | None = None,
        proposal_hash: str | None = None,
        payload: dict[str, Any] | None = None,
        supersedes_id: str | None = None,
    ) -> DecisionRecord:
        """Validate and append one decision (no commit). Unknown enums raise ``ValueError``."""
        rec = DecisionRecord(
            id=_new_id("dec"),
            chain_run_id=chain_run_id,
            run_id=run_id,
            persona=JournalPersona(persona),
            stage=Stage(stage),
            subject=subject,
            choice=Choice(choice),
            reason_code=ReasonCode(reason_code),
            reason_text=reason_text,
            confidence=confidence,
            inputs_snapshot_id=inputs_snapshot_id,
            persona_call_id=persona_call_id,
            proposal_hash=proposal_hash,
            payload=payload or {},
            supersedes_id=supersedes_id,
            at=at,
        )
        self.conn.execute(
            """INSERT INTO decisions
               (id, chain_run_id, run_id, persona, stage, subject, choice, reason_code,
                reason_text, confidence, inputs_snapshot_id, persona_call_id, proposal_hash,
                payload, supersedes_id, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rec.id,
                rec.chain_run_id,
                rec.run_id,
                str(rec.persona),
                str(rec.stage),
                rec.subject,
                str(rec.choice),
                str(rec.reason_code),
                rec.reason_text,
                rec.confidence,
                rec.inputs_snapshot_id,
                rec.persona_call_id,
                rec.proposal_hash,
                json.dumps(rec.payload, sort_keys=True, default=str),
                rec.supersedes_id,
                to_db(rec.at),
            ),
        )
        return rec

    def decisions(
        self,
        *,
        chain_run_id: str | None = None,
        proposal_hash: str | None = None,
        since: _dt.datetime | None = None,
    ) -> list[DecisionRecord]:
        """Decisions matching every given filter, in stage order then time order."""
        sql = "SELECT * FROM decisions WHERE 1 = 1"
        params: list[str] = []
        if chain_run_id is not None:
            sql += " AND chain_run_id = ?"
            params.append(chain_run_id)
        if proposal_hash is not None:
            sql += " AND proposal_hash = ?"
            params.append(proposal_hash)
        if since is not None:
            sql += " AND at >= ?"
            params.append(to_db(since))
        rows = self.conn.execute(sql + " ORDER BY at, rowid", params).fetchall()
        recs = [self._row(r) for r in rows]
        order = {s: i for i, s in enumerate(STAGE_ORDER)}
        return sorted(recs, key=lambda d: order[d.stage])  # stable: time order within a stage

    def get(self, decision_id: str) -> DecisionRecord | None:
        row = self.conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> DecisionRecord:
        at = from_db(row["at"])
        # D54: pre-rename rows say persona 'scout' / reason 'scout_candidate' (the Sweep).
        # A journal row has no store handle here, so 'scout' always maps to the Sweep until
        # the new Scout persona (E5.13) adds its own enum value and cutover-aware read.
        return DecisionRecord(
            id=row["id"],
            chain_run_id=row["chain_run_id"],
            run_id=row["run_id"],
            persona=JournalPersona(legacy.persona_key(row["persona"], at, None)),
            stage=Stage(row["stage"]),
            subject=row["subject"],
            choice=Choice(row["choice"]),
            reason_code=ReasonCode(legacy.reason_code(row["reason_code"])),
            reason_text=row["reason_text"],
            confidence=row["confidence"],
            inputs_snapshot_id=row["inputs_snapshot_id"],
            persona_call_id=row["persona_call_id"],
            proposal_hash=row["proposal_hash"],
            payload=json.loads(row["payload"]),
            supersedes_id=row["supersedes_id"],
            at=at,
        )

    # -- chain lookups -------------------------------------------------------

    def chain_for_proposal(self, proposal_hash: str) -> str | None:
        row = self.conn.execute(
            """SELECT r.chain_run_id FROM proposals p
               JOIN routine_runs r ON r.run_id = p.run_id
               WHERE p.proposal_hash = ?""",
            (proposal_hash,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def proposals_in_chain(self, chain_run_id: str) -> list[str]:
        rows = self.conn.execute(
            """SELECT p.proposal_hash FROM proposals p
               JOIN routine_runs r ON r.run_id = p.run_id
               WHERE r.chain_run_id = ? ORDER BY p.created_at, p.rowid""",
            (chain_run_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def resolve(self, ref: str) -> tuple[str | None, list[str]]:
        """``(chain_run_id, proposal_hashes)`` for a proposal hash (or prefix) or a chain id."""
        if ref.startswith("chain-"):
            return ref, self.proposals_in_chain(ref)
        rows = self.conn.execute(
            "SELECT proposal_hash FROM proposals WHERE proposal_hash LIKE ? || '%'", (ref,)
        ).fetchall()
        if len(rows) != 1:
            msg = (
                f"no proposal matches {ref!r}"
                if not rows
                else f"{ref!r} is ambiguous ({len(rows)} proposals)"
            )
            raise LookupError(msg)
        phash = str(rows[0][0])
        return self.chain_for_proposal(phash), [phash]

    # -- market context ------------------------------------------------------

    def record_market_context(self, mc: MarketContext) -> str:
        row_id = _new_id("mkt")
        self.conn.execute(
            """INSERT INTO market_contexts (id, proposal_hash, payload, quotes_as_of, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                row_id,
                mc.proposal_hash,
                mc.model_dump_json(),
                to_db(mc.quotes_as_of) if mc.quotes_as_of else None,
                to_db(mc.at),
            ),
        )
        return row_id

    def market_context(self, proposal_hash: str) -> MarketContext | None:
        row = self.conn.execute(
            """SELECT payload FROM market_contexts WHERE proposal_hash = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (proposal_hash,),
        ).fetchone()
        return MarketContext.model_validate_json(row[0]) if row else None

    # -- outcomes ------------------------------------------------------------

    def record_outcome(self, o: OutcomeRecord) -> str:
        row_id = _new_id("out")
        self.conn.execute(
            """INSERT INTO outcomes
               (id, proposal_hash, status, contracts, limit_price, entry_fill, slippage_usd,
                slippage_bps, cost_bps, exit_fill, realised_pnl, max_adverse_excursion,
                days_held, exit_reason, ev_total, pnl_vs_ev, hold_to_expiry_shadow_pnl,
                supersedes_id, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                o.proposal_hash,
                str(o.status),
                o.contracts,
                _dec(o.limit_price),
                _dec(o.entry_fill),
                _dec(o.slippage_usd),
                o.slippage_bps,
                o.cost_bps,
                _dec(o.exit_fill),
                _dec(o.realised_pnl),
                _dec(o.max_adverse_excursion),
                o.days_held,
                o.exit_reason,
                _dec(o.ev_total),
                _dec(o.pnl_vs_ev),
                _dec(o.hold_to_expiry_shadow_pnl),
                o.supersedes_id,
                to_db(o.at),
            ),
        )
        return row_id

    def outcome(self, proposal_hash: str) -> OutcomeRecord | None:
        """The latest outcome row for *proposal_hash* (later rows supersede earlier ones)."""
        row = self.conn.execute(
            """SELECT * FROM outcomes WHERE proposal_hash = ?
               ORDER BY at DESC, rowid DESC LIMIT 1""",
            (proposal_hash,),
        ).fetchone()
        return self._outcome(row) if row else None

    def latest_outcome_row(self, proposal_hash: str) -> tuple[str, str] | None:
        """``(id, status)`` of the latest outcome row for *proposal_hash*, if any."""
        row = self.conn.execute(
            """SELECT id, status FROM outcomes WHERE proposal_hash = ?
               ORDER BY at DESC, rowid DESC LIMIT 1""",
            (proposal_hash,),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def outcomes(self, *, since: _dt.datetime | None = None) -> list[OutcomeRecord]:
        """Latest outcome per proposal."""
        sql = """SELECT o.* FROM outcomes o
                 WHERE o.rowid = (SELECT o2.rowid FROM outcomes o2
                                  WHERE o2.proposal_hash = o.proposal_hash
                                  ORDER BY o2.at DESC, o2.rowid DESC LIMIT 1)"""
        params: list[str] = []
        if since is not None:
            sql += " AND o.at >= ?"
            params.append(to_db(since))
        return [self._outcome(r) for r in self.conn.execute(sql + " ORDER BY o.at", params)]

    @staticmethod
    def _outcome(row: sqlite3.Row) -> OutcomeRecord:
        def d(key: str) -> Decimal | None:
            return None if row[key] is None else Decimal(row[key])

        return OutcomeRecord(
            proposal_hash=row["proposal_hash"],
            status=row["status"],
            contracts=row["contracts"],
            limit_price=d("limit_price"),
            entry_fill=d("entry_fill"),
            slippage_usd=d("slippage_usd"),
            slippage_bps=row["slippage_bps"],
            cost_bps=row["cost_bps"],
            exit_fill=d("exit_fill"),
            realised_pnl=d("realised_pnl"),
            max_adverse_excursion=d("max_adverse_excursion"),
            days_held=row["days_held"],
            exit_reason=row["exit_reason"],
            ev_total=d("ev_total"),
            pnl_vs_ev=d("pnl_vs_ev"),
            hold_to_expiry_shadow_pnl=d("hold_to_expiry_shadow_pnl"),
            supersedes_id=row["supersedes_id"],
            at=from_db(row["at"]),
        )

    # -- reviews -------------------------------------------------------------

    def add_review(self, review: DecisionReview) -> str:
        """Append a review (own transaction). Every cited decision must exist.

        This is the only journal write open to the Auditor (LLM): it can label
        decisions, never create or change them.
        """
        cites = list(dict.fromkeys(review.cites))
        if review.decision_id and review.decision_id not in cites:
            cites.insert(0, review.decision_id)
        if not cites:
            msg = "a review must cite at least one decision id"
            raise ReviewCitationError(msg)
        missing = [c for c in cites if self.get(c) is None]
        if missing:
            msg = f"review cites unknown decision(s): {', '.join(missing)}"
            raise ReviewCitationError(msg)
        if review.proposal_hash is not None:
            known = self.conn.execute(
                "SELECT 1 FROM proposals WHERE proposal_hash = ?", (review.proposal_hash,)
            ).fetchone()
            if known is None:
                msg = f"review targets unknown proposal {review.proposal_hash[:12]}"
                raise ReviewCitationError(msg)
        row_id = review.id or _new_id("rev")
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO decision_reviews
                       (id, proposal_hash, decision_id, label, root_cause, notes, reviewer,
                        supersedes_id, at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row_id,
                        review.proposal_hash,
                        review.decision_id,
                        str(review.label),
                        str(review.root_cause),
                        review.notes,
                        str(review.reviewer),
                        review.supersedes_id,
                        to_db(review.at),
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO decision_review_citations (review_id, decision_id) VALUES (?, ?)",
                    [(row_id, c) for c in cites],
                )
        except sqlite3.IntegrityError as exc:  # FK backstop (PRAGMA foreign_keys = ON)
            raise ReviewCitationError(str(exc)) from exc
        return row_id

    def reviews(
        self, *, proposal_hash: str | None = None, since: _dt.datetime | None = None
    ) -> list[DecisionReview]:
        """Reviews, excluding any that a later review supersedes."""
        sql = """SELECT * FROM decision_reviews r
                 WHERE NOT EXISTS (SELECT 1 FROM decision_reviews s WHERE s.supersedes_id = r.id)"""
        params: list[str] = []
        if proposal_hash is not None:
            sql += """ AND (r.proposal_hash = ? OR r.decision_id IN
                         (SELECT id FROM decisions WHERE proposal_hash = ?))"""
            params += [proposal_hash, proposal_hash]
        if since is not None:
            sql += " AND r.at >= ?"
            params.append(to_db(since))
        out: list[DecisionReview] = []
        for row in self.conn.execute(sql + " ORDER BY r.at, r.rowid", params).fetchall():
            cites = [
                c[0]
                for c in self.conn.execute(
                    "SELECT decision_id FROM decision_review_citations WHERE review_id = ? "
                    "ORDER BY rowid",
                    (row["id"],),
                )
            ]
            out.append(
                DecisionReview(
                    id=row["id"],
                    proposal_hash=row["proposal_hash"],
                    decision_id=row["decision_id"],
                    label=row["label"],
                    root_cause=row["root_cause"],
                    notes=row["notes"],
                    reviewer=row["reviewer"],
                    cites=cites,
                    supersedes_id=row["supersedes_id"],
                    at=from_db(row["at"]),
                )
            )
        return out

    # -- persona calls -------------------------------------------------------

    def persona_calls(self, chain_run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT c.* FROM persona_calls c JOIN routine_runs r ON r.run_id = c.run_id
               WHERE r.chain_run_id = ? ORDER BY c.created_at, c.rowid""",
            (chain_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


class Recorder:
    """A :class:`JournalStore` bound to one run: chain, run, time and input snapshot.

    Pipeline steps use one per run so every record carries the same audit ids.
    Like :meth:`JournalStore.record` it never commits.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        at: _dt.datetime,
        chain_run_id: str | None = None,
        run_id: str | None = None,
        inputs_snapshot_id: str | None = None,
    ) -> None:
        self.store = JournalStore(conn)
        self.at = at
        self.chain_run_id = chain_run_id
        self.run_id = run_id
        self.inputs_snapshot_id = inputs_snapshot_id
        self.records: list[DecisionRecord] = []

    def add(
        self,
        persona: JournalPersona | str,
        stage: Stage | str,
        subject: str,
        choice: Choice | str,
        reason_code: ReasonCode | str,
        **kwargs: Any,
    ) -> DecisionRecord:
        kwargs.setdefault("inputs_snapshot_id", self.inputs_snapshot_id)
        rec = self.store.record(
            persona=persona,
            stage=stage,
            subject=subject,
            choice=choice,
            reason_code=reason_code,
            at=self.at,
            chain_run_id=self.chain_run_id,
            run_id=self.run_id,
            **kwargs,
        )
        self.records.append(rec)
        return rec
