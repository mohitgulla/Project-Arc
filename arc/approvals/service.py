"""Approval requests: post the card, record the click, enforce the TTL (E6.1).

Lifecycle of one proposal (one row in ``approval_requests``)::

    publish ──► pending ──► approved   (allowed approver clicked Approve before expiry)
       │           ├──────► rejected   (allowed approver clicked Reject)
       │           └──────► expired    (TTL passed; expired = rejected)
       └──────► not_actionable         (gate FAIL or no gate token: info card, no buttons)

Rules enforced here, in code:

- Only Slack user ids in ``ARC_APPROVER_SLACK_USER_IDS`` (default: the owner,
  D10) can decide. Other clicks are refused and change nothing.
- A decision is accepted only while ``now < proposal.expires_at`` (the TTL,
  ``ARC_APPROVAL_TTL_SECONDS``, default 20 min). A late click expires the
  request instead of approving it.
- A request resolves exactly once. The decision is written as an
  :class:`~arc.models.ApprovalRecord` row in ``approvals`` in the same
  transaction that closes the request; a unique index on
  ``approvals.proposal_hash`` backs this up.
- The stored proposal is re-hashed on load, so the ApprovalRecord is bound to
  the exact payload the gate saw.
- ``auto_approve`` (D34, one switch per environment, default off) approves an
  actionable request at publish time, as ``arc:auto-approve``. The card is still
  posted, marked ``Auto-approved (paper|LIVE)`` and not actionable.
- E7.5a scorecard gate (``auto_approve_scorecard_gate``, default on): D34 only
  auto-approves an *open* when the E7.3 scorecard shows enough closed trades,
  realised net EV >= 0 and entry slippage within tolerance
  (:func:`arc.journal.scorecard.auto_approve_readiness`). Otherwise the request
  stays pending with its buttons (manual approval) and an ``auto_approve_gated``
  journal row names the failing criteria. Closes are never gated (reducing risk).
  Turning the gate off is an explicit opt-out, logged as a warning each time.
- Every rejection and expiry is logged (``approvals.rejected`` /
  ``approvals.expired``) with its reason.
- Every resolution (and every not-actionable card) is written to the decision
  journal (E7.4) in the same transaction as the request row. An owner may add
  an optional reject reason afterwards (:meth:`ApprovalService.record_reason`,
  the Slack modal): it is a follow-up journal record, the decision itself was
  already recorded on the click.

Slack is best-effort: state is committed before anything is posted, so a Slack
failure never changes a decision.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from arc.approvals.card import CardView, render_card, render_resolved, ticker_of
from arc.approvals.trail import load_trail
from arc.context.ttl import from_db, require_aware, to_db
from arc.gate.rules import proposal_hash as hash_proposal
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.scorecard import AutoApproveReadiness, auto_approve_readiness
from arc.journal.store import JournalStore
from arc.models import ApprovalDecision, ApprovalRecord, GateDecision, Proposal
from arc.structures import is_defined_risk
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Sequence

    from arc.config import ArcSettings

log = structlog.get_logger(__name__)

__all__ = [
    "AUTO_APPROVER",
    "TTL_ACTOR",
    "ApprovalService",
    "CardPoster",
    "DecideResult",
    "LogCardPoster",
    "Outcome",
    "PostedCard",
    "ReasonResult",
    "RequestStatus",
    "SweepReport",
    "approval_record",
]

TTL_ACTOR = "arc:ttl"
AUTO_APPROVER = "arc:auto-approve"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    NOT_ACTIONABLE = "not_actionable"


class Outcome(StrEnum):
    """Result of a click (:meth:`ApprovalService.decide`). Stable, machine-readable."""

    APPROVED = "approved"
    REJECTED = "rejected"
    UNAUTHORIZED = "unauthorized"
    EXPIRED = "expired"
    ALREADY_DECIDED = "already_decided"
    NOT_ACTIONABLE = "not_actionable"
    UNKNOWN = "unknown_proposal"


@dataclass(frozen=True)
class DecideResult:
    outcome: Outcome
    proposal_hash: str
    message: str
    status: RequestStatus | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome in (Outcome.APPROVED, Outcome.REJECTED)


@dataclass(frozen=True)
class ReasonResult:
    """Result of :meth:`ApprovalService.record_reason` (the optional reject reason)."""

    outcome: str  # recorded | blank | unauthorized | not_rejected | unknown_proposal
    proposal_hash: str
    message: str
    decision_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome in ("recorded", "blank")


@dataclass(frozen=True)
class PostedCard:
    channel: str
    thread_ts: str | None
    message_ts: str | None


class CardPoster(Protocol):
    """Where cards go: the #arc-investor day thread, or the log (dry runs)."""

    def post(self, day: _dt.date, view: CardView) -> PostedCard: ...

    def update(self, channel: str, message_ts: str, view: CardView) -> None: ...

    def notify_user(self, channel: str, user: str, text: str, thread_ts: str | None) -> None: ...


class LogCardPoster:
    """Writes cards to the structured log only (dry runs, tests, --no-slack)."""

    def __init__(self) -> None:
        self.posted: list[tuple[_dt.date, CardView]] = []
        self.updated: list[tuple[str, str, CardView]] = []
        self.notices: list[tuple[str, str]] = []

    def post(self, day: _dt.date, view: CardView) -> PostedCard:
        self.posted.append((day, view))
        log.info("approvals.card", day=day.isoformat(), text=view.text)
        return PostedCard(channel="log", thread_ts=None, message_ts=None)

    def update(self, channel: str, message_ts: str, view: CardView) -> None:
        self.updated.append((channel, message_ts, view))
        log.info("approvals.card_update", text=view.text)

    def notify_user(self, channel: str, user: str, text: str, thread_ts: str | None) -> None:
        self.notices.append((user, text))
        log.info("approvals.notice", user=user, text=text)


@dataclass
class SweepReport:
    published: list[str]
    auto_approved: list[str]
    expired: list[str]
    auto_gated: list[str] = field(default_factory=list)  # E7.5a: left for a manual click

    def as_json(self) -> dict[str, list[str]]:
        return {
            "published": self.published,
            "auto_approved": self.auto_approved,
            "auto_gated": self.auto_gated,
            "expired": self.expired,
        }


@dataclass(frozen=True)
class _Request:
    proposal_hash: str
    ticker: str
    day: str
    proposal: Proposal
    status: RequestStatus
    reason: str
    channel: str
    thread_ts: str | None
    message_ts: str | None
    expires_at: _dt.datetime


def _load_proposal(raw: str, expected_hash: str) -> Proposal:
    proposal = Proposal.model_validate_json(raw)
    actual = hash_proposal(proposal)
    if actual != expected_hash:
        msg = f"stored proposal hashes to {actual[:12]}, not {expected_hash[:12]}"
        raise ValueError(msg)
    return proposal


def _row_to_request(row: sqlite3.Row) -> _Request:
    return _Request(
        proposal_hash=row["proposal_hash"],
        ticker=row["ticker"],
        day=row["day"],
        proposal=_load_proposal(row["proposal_json"], row["proposal_hash"]),
        status=RequestStatus(row["status"]),
        reason=row["reason"],
        channel=row["channel"],
        thread_ts=row["thread_ts"],
        message_ts=row["message_ts"],
        expires_at=from_db(row["expires_at"]),
    )


def _proposal_from_row(row: sqlite3.Row) -> Proposal:
    """Rebuild the gated Proposal from a ``proposals`` row + its context entry.

    The ``proposals`` table keeps the gated fields as JSON columns, but not
    ``limit_price`` / earnings flags. The authoritative full payload is the
    ``proposal`` context entry the propose step wrote in the same run.
    """
    ctx = row["context_payload"]
    if ctx is None:
        msg = f"no proposal context entry for {row['proposal_hash'][:12]}"
        raise LookupError(msg)
    return _load_proposal(ctx, row["proposal_hash"])


def _gate_decision(row: sqlite3.Row) -> GateDecision | None:
    if row["gate_passed"] is None:
        return None
    return GateDecision(
        proposal_hash=row["proposal_hash"],
        passed=bool(row["gate_passed"]),
        violations=json.loads(row["gate_violations"] or "[]"),
        token=row["gate_token"],
    )


def approval_record(conn: sqlite3.Connection, proposal_hash: str) -> ApprovalRecord | None:
    """The ApprovalRecord for *proposal_hash*, if one was written (E6.2 reads this)."""
    row = conn.execute(
        """SELECT proposal_hash, slack_user, slack_ts, decision, decided_at
           FROM approvals WHERE proposal_hash = ?""",
        (proposal_hash,),
    ).fetchone()
    if row is None:
        return None
    return ApprovalRecord(
        proposal_hash=row["proposal_hash"],
        slack_user=row["slack_user"],
        slack_ts=row["slack_ts"],
        decision=ApprovalDecision(row["decision"]),
        at=from_db(row["decided_at"]),
    )


class ApprovalService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: ArcSettings,
        poster: CardPoster,
        *,
        live: bool = False,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.poster = poster
        # E5.2b: a live sweep (Slack) explains a token-less PASS as a missing
        # secret; an offline one (dry run / fixtures) as the expected no-permission.
        self.live = live

    # -- queries -------------------------------------------------------------

    @property
    def approvers(self) -> frozenset[str]:
        return frozenset(self.settings.approver_slack_user_ids)

    def _request(self, proposal_hash: str) -> _Request | None:
        row = self.conn.execute(
            "SELECT * FROM approval_requests WHERE proposal_hash = ?", (proposal_hash,)
        ).fetchone()
        return _row_to_request(row) if row else None

    def _decision_for(self, proposal_hash: str) -> GateDecision | None:
        row = self.conn.execute(
            """SELECT proposal_hash, passed AS gate_passed, violations_json AS gate_violations,
                      token AS gate_token
               FROM gate_decisions WHERE proposal_hash = ?
               ORDER BY decided_at DESC, rowid DESC LIMIT 1""",
            (proposal_hash,),
        ).fetchone()
        return _gate_decision(row) if row else None

    def requests(self, *, day: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM approval_requests"
        params: tuple[str, ...] = ()
        if day:
            sql += " WHERE day = ?"
            params = (day,)
        rows = self.conn.execute(sql + " ORDER BY created_at, rowid", params).fetchall()
        return [{k: v for k, v in dict(r).items() if k != "proposal_json"} for r in rows]

    # -- publish -------------------------------------------------------------

    def _unpublished(self, day: str | None, only: Sequence[str] | None = None) -> list[sqlite3.Row]:
        sql = """
            SELECT p.proposal_hash, p.ticker, p.day, p.run_id, p.kind,
                   g.passed AS gate_passed, g.violations_json AS gate_violations,
                   g.token AS gate_token,
                   (SELECT c.payload FROM context_entries c
                     WHERE c.kind = 'proposal' AND c.run_id = p.run_id AND c.subject = p.ticker
                     ORDER BY c.created_at DESC, c.rowid DESC LIMIT 1) AS context_payload
            FROM proposals p
            LEFT JOIN gate_decisions g ON g.id = (
                SELECT id FROM gate_decisions WHERE proposal_hash = p.proposal_hash
                ORDER BY decided_at DESC, rowid DESC LIMIT 1)
            WHERE p.day IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM approval_requests r
                              WHERE r.proposal_hash = p.proposal_hash)"""
        params: list[str] = []
        if day:
            sql += " AND p.day = ?"
            params.append(day)
        if only is not None:
            if not only:
                return []
            sql += f" AND p.proposal_hash IN ({','.join('?' * len(only))})"
            params.extend(only)
        return self.conn.execute(sql + " ORDER BY p.created_at, p.rowid", params).fetchall()

    def publish_pending(
        self,
        now: _dt.datetime,
        *,
        day: str | None = None,
        only: Sequence[str] | None = None,
    ) -> SweepReport:
        """Post a card for every proposal that has none yet; auto-approve if enabled.

        ``only`` restricts the sweep to those proposal hashes (D34: the in-chain
        ``execute`` step publishes its own chain's proposals through this same path;
        the later tick sweep then finds nothing left for them, per proposal_hash).
        """
        now = require_aware(now, "now").astimezone(ET)
        report = SweepReport(published=[], auto_approved=[], expired=[])
        self._readiness = None  # E7.5a: computed once per sweep, on first need
        for row in self._unpublished(day, only):
            phash = row["proposal_hash"]
            try:
                proposal = _proposal_from_row(row)
            except (LookupError, ValueError) as exc:
                log.error("approvals.unpublishable", proposal_hash=phash, error=str(exc))
                continue
            decision = _gate_decision(row)
            status, reason = self._initial_status(proposal, decision, now)
            if not self._insert_request(phash, row, proposal, status, reason, now):
                continue  # a concurrent sweep won
            actionable = status is RequestStatus.PENDING
            auto = self._auto_reason(proposal, row["kind"]) if actionable else None
            gated: AutoApproveReadiness | None = None
            if auto is not None and auto.startswith("auto-approve"):
                auto, gated = self._scorecard_gate(phash, row, auto, now)
            # D34: an auto-approved card posts without buttons, marked as such.
            view = render_card(
                proposal,
                decision,
                proposal_hash=phash,
                actionable=actionable and auto is None,
                note=self._auto_label()
                if auto is not None
                else (_gated_note(gated) if gated is not None else reason),
                trail=load_trail(self.conn, phash, row["ticker"]),
                kind=row["kind"],
            )
            day_date = _dt.date.fromisoformat(row["day"])
            posted = self._post(day_date, view, phash)
            if posted is not None:
                with self.conn:
                    self.conn.execute(
                        """UPDATE approval_requests SET channel = ?, thread_ts = ?, message_ts = ?
                           WHERE proposal_hash = ?""",
                        (posted.channel, posted.thread_ts, posted.message_ts, phash),
                    )
            report.published.append(phash)
            log.info(
                "approvals.published",
                proposal_hash=phash,
                ticker=row["ticker"],
                status=str(status),
                reason=reason,
                expires_at=proposal.expires_at.isoformat(),
            )
            if status is RequestStatus.NOT_ACTIONABLE:
                log.info(
                    "approvals.rejected", proposal_hash=phash, ticker=row["ticker"], reason=reason
                )
            if gated is not None:
                report.auto_gated.append(phash)
            if auto is not None:
                res = self._resolve(phash, RequestStatus.APPROVED, AUTO_APPROVER, auto, now)
                if res.outcome is Outcome.APPROVED:
                    report.auto_approved.append(phash)
        return report

    # -- E7.5a scorecard gate ------------------------------------------------

    _readiness: AutoApproveReadiness | None = None

    def readiness(self, now: _dt.datetime) -> AutoApproveReadiness:
        """The scorecard gate's verdict at *now* (cached for one sweep)."""
        if self._readiness is None:
            self._readiness = auto_approve_readiness(
                self.conn,
                now=now,
                min_closed_trades=self.settings.auto_approve_min_closed_trades,
                slippage_tolerance=self.settings.auto_approve_slippage_tolerance,
            )
        return self._readiness

    def _scorecard_gate(
        self, phash: str, row: sqlite3.Row, auto: str, now: _dt.datetime
    ) -> tuple[str | None, AutoApproveReadiness | None]:
        """E7.5a: keep the D34 auto reason, or drop it (and journal why) for an open.

        Returns ``(auto reason or None, readiness when gated)``. Closes pass through.
        """
        if row["kind"] == "close":
            return auto, None
        env = self.settings.env.value
        if not self.settings.auto_approve_scorecard_gate:
            log.warning(
                "approvals.auto_approve_scorecard_gate_off",
                proposal_hash=phash,
                env=env,
                note="auto_approve.scorecard_gate is off: auto-approving with no realised-"
                "performance check (explicit opt-out)",
            )
            return f"{auto[:-1]}, scorecard gate off)", None
        ready = self.readiness(now)
        if ready.ok:
            return f"{auto[:-1]}, scorecard gate met)", None
        text = f"auto-approve gated ({env}): {ready.summary()}"
        with self.conn:
            self._journal(
                phash,
                persona=JournalPersona.SYSTEM,
                choice=Choice.NOTED,
                code=ReasonCode.AUTO_APPROVE_GATED,
                text=text,
                now=now,
                subject=row["ticker"],
                run_id=row["run_id"],
                payload={"env": env, **ready.model_dump(mode="json")},
            )
        log.info(
            "approvals.auto_approve_gated",
            proposal_hash=phash,
            ticker=row["ticker"],
            env=env,
            failing=ready.failing,
            detail=ready.summary(),
        )
        return None, ready

    def _auto_label(self) -> str:
        """``Auto-approved (paper)`` / ``Auto-approved (LIVE)`` (D34 card marker)."""
        env = self.settings.env.value
        return f"Auto-approved ({'LIVE' if env == 'live' else env})"

    def _auto_approve_enabled(self) -> bool:
        """D34: the per-env switch. ``settings.auto_approve`` already *is* the value
        for the running ``ARC_ENV`` (the env var is paper-only; live comes from the
        config store only, see ``ArcSettings._env_switches_paper_only``)."""
        return bool(self.settings.auto_approve)

    def _auto_reason(self, proposal: Proposal, kind: str) -> str | None:
        """Why this actionable proposal is approved without a click, else None.

        D34 ``auto_approve`` (per environment, default off) covers everything;
        D24 ``auto_exit_defined_risk`` (per environment, default off) covers exits
        whose closing legs are defined risk.
        """
        env = self.settings.env.value
        if self._auto_approve_enabled():
            return f"auto-approve ({env}, D34)"
        if (
            kind == "close"
            and self.settings.auto_exit_defined_risk
            and is_defined_risk(proposal.structure.legs)
        ):
            return f"auto-exit (defined risk, {env}, D24)"
        return None

    def _initial_status(
        self, proposal: Proposal, decision: GateDecision | None, now: _dt.datetime
    ) -> tuple[RequestStatus, str]:
        if decision is None:
            return RequestStatus.NOT_ACTIONABLE, "no gate decision"
        if not decision.passed:
            return RequestStatus.NOT_ACTIONABLE, "gate failed: " + "; ".join(decision.violations)
        if not decision.token:
            if self.live:
                return (
                    RequestStatus.NOT_ACTIONABLE,
                    "no gate token (ARC_GATE_SECRET missing when proposed): not executable",
                )
            return (
                RequestStatus.NOT_ACTIONABLE,
                "no gate token (dry run / fixtures): informational only",
            )
        if proposal.expires_at <= now:
            return RequestStatus.NOT_ACTIONABLE, "proposal expired before its card was posted"
        return RequestStatus.PENDING, ""

    def _insert_request(
        self,
        phash: str,
        row: sqlite3.Row,
        proposal: Proposal,
        status: RequestStatus,
        reason: str,
        now: _dt.datetime,
    ) -> bool:
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO approval_requests
                       (proposal_hash, ticker, day, proposal_json, status, reason, channel,
                        expires_at, created_at, decided_at, decided_by, run_id)
                       VALUES (?, ?, ?, ?, ?, ?, 'log', ?, ?, ?, ?, ?)""",
                    (
                        phash,
                        row["ticker"] or ticker_of(proposal),
                        row["day"],
                        proposal.model_dump_json(),
                        str(status),
                        reason,
                        to_db(proposal.expires_at),
                        to_db(now),
                        to_db(now) if status is RequestStatus.NOT_ACTIONABLE else None,
                        "arc:gate" if status is RequestStatus.NOT_ACTIONABLE else None,
                        row["run_id"],
                    ),
                )
                if status is RequestStatus.NOT_ACTIONABLE:
                    self._journal(
                        phash,
                        persona=JournalPersona.SYSTEM,
                        choice=Choice.NO_TRADE,
                        code=_not_actionable_code(reason),
                        text=reason,
                        now=now,
                        subject=row["ticker"] or ticker_of(proposal),
                        run_id=row["run_id"],
                    )
        except sqlite3.IntegrityError:
            return False
        return True

    def _journal(
        self,
        phash: str,
        *,
        persona: JournalPersona,
        choice: Choice,
        code: ReasonCode,
        text: str,
        now: _dt.datetime,
        subject: str | None = None,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
        supersedes_id: str | None = None,
    ) -> str:
        """Append an approval-stage decision (no commit: the caller's transaction)."""
        journal = JournalStore(self.conn)
        if subject is None or run_id is None:
            row = self.conn.execute(
                "SELECT ticker, run_id FROM approval_requests WHERE proposal_hash = ?", (phash,)
            ).fetchone()
            subject = subject or (row["ticker"] if row else "session")
            run_id = run_id or (row["run_id"] if row else None)
        return journal.record(
            persona=persona,
            stage=Stage.APPROVAL,
            subject=subject,
            choice=choice,
            reason_code=code,
            reason_text=text,
            at=now,
            chain_run_id=journal.chain_for_proposal(phash),
            run_id=run_id,
            proposal_hash=phash,
            payload=payload or {},
            supersedes_id=supersedes_id,
        ).id

    def _post(self, day: _dt.date, view: CardView, phash: str) -> PostedCard | None:
        try:
            return self.poster.post(day, view)
        except Exception as exc:  # noqa: BLE001 - state is committed; posting is best-effort
            log.error("approvals.post_failed", proposal_hash=phash, error=str(exc))
            return None

    # -- decide --------------------------------------------------------------

    def decide(
        self,
        proposal_hash: str,
        *,
        user: str,
        approve: bool,
        now: _dt.datetime,
        slack_ts: str = "",
    ) -> DecideResult:
        """Apply an Approve / Reject click by Slack user *user* (id from the platform)."""
        now = require_aware(now, "now").astimezone(ET)
        req = self._request(proposal_hash)
        if req is None:
            return DecideResult(Outcome.UNKNOWN, proposal_hash, "Unknown proposal.")
        if not user or user not in self.approvers:
            log.warning(
                "approvals.unauthorized", proposal_hash=proposal_hash, user=user or "unknown"
            )
            self._notify(req, user, "⛔ You are not an allowed approver; nothing was recorded.")
            return DecideResult(
                Outcome.UNAUTHORIZED,
                proposal_hash,
                f"<@{user}> is not an allowed approver.",
                req.status,
            )
        if req.status is RequestStatus.NOT_ACTIONABLE:
            return DecideResult(
                Outcome.NOT_ACTIONABLE,
                proposal_hash,
                f"This proposal is not actionable ({req.reason}).",
                req.status,
            )
        if req.status is not RequestStatus.PENDING:
            return DecideResult(
                Outcome.ALREADY_DECIDED,
                proposal_hash,
                f"Already {req.status}.",
                req.status,
            )
        if now >= req.expires_at or now >= req.proposal.expires_at:
            self._resolve(proposal_hash, RequestStatus.EXPIRED, TTL_ACTOR, _ttl_reason(req), now)
            self._notify(req, user, "⏳ Too late: this proposal expired and counts as rejected.")
            return DecideResult(
                Outcome.EXPIRED, proposal_hash, "Expired before the click.", RequestStatus.EXPIRED
            )
        if approve:
            return self._resolve(
                proposal_hash, RequestStatus.APPROVED, user, "", now, slack_ts=slack_ts
            )
        return self._resolve(
            proposal_hash,
            RequestStatus.REJECTED,
            user,
            f"rejected by <@{user}>",
            now,
            slack_ts=slack_ts,
        )

    def _notify(self, req: _Request, user: str, text: str) -> None:
        if not user or req.channel == "log":
            return
        try:
            self.poster.notify_user(req.channel, user, text, req.thread_ts)
        except Exception as exc:  # noqa: BLE001
            log.warning("approvals.notice_failed", error=str(exc))

    # -- expire --------------------------------------------------------------

    def expire_due(self, now: _dt.datetime) -> list[str]:
        """Expire every pending request whose TTL has passed (expired = rejected)."""
        now = require_aware(now, "now").astimezone(ET)
        rows = self.conn.execute(
            """SELECT proposal_hash FROM approval_requests
               WHERE status = 'pending' AND expires_at <= ? ORDER BY expires_at""",
            (to_db(now),),
        ).fetchall()
        expired: list[str] = []
        for r in rows:
            req = self._request(r["proposal_hash"])
            if req is None:  # pragma: no cover - row just read
                continue
            res = self._resolve(
                req.proposal_hash, RequestStatus.EXPIRED, TTL_ACTOR, _ttl_reason(req), now
            )
            if res.outcome is Outcome.EXPIRED:
                expired.append(req.proposal_hash)
        return expired

    def sweep(self, now: _dt.datetime, *, day: str | None = None) -> SweepReport:
        """Publish new cards, then expire overdue ones (run on every tick)."""
        report = self.publish_pending(now, day=day)
        report.expired = self.expire_due(now)
        return report

    # -- resolution ----------------------------------------------------------

    def _resolve(
        self,
        proposal_hash: str,
        status: RequestStatus,
        actor: str,
        reason: str,
        now: _dt.datetime,
        *,
        slack_ts: str = "",
    ) -> DecideResult:
        decision = {
            RequestStatus.APPROVED: ApprovalDecision.APPROVED,
            RequestStatus.REJECTED: ApprovalDecision.REJECTED,
            RequestStatus.EXPIRED: ApprovalDecision.EXPIRED,
        }[status]
        approval_id = f"apr-{uuid.uuid4().hex[:16]}"
        try:
            with self.conn:
                req_row = self.conn.execute(
                    "SELECT message_ts FROM approval_requests WHERE proposal_hash = ?",
                    (proposal_hash,),
                ).fetchone()
                # The ApprovalRecord first (unique per proposal), then close the request;
                # either failing rolls back both, so a request resolves exactly once.
                self.conn.execute(
                    """INSERT INTO approvals
                       (id, proposal_hash, slack_user, slack_ts, decision, decided_at, run_id)
                       VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        approval_id,
                        proposal_hash,
                        actor,
                        slack_ts or ((req_row["message_ts"] if req_row else None) or ""),
                        str(decision),
                        to_db(now),
                    ),
                )
                cur = self.conn.execute(
                    """UPDATE approval_requests
                       SET status = ?, reason = ?, decided_at = ?, decided_by = ?, approval_id = ?
                       WHERE proposal_hash = ? AND status = 'pending'""",
                    (str(status), reason, to_db(now), actor, approval_id, proposal_hash),
                )
                if cur.rowcount != 1:
                    raise _LostRaceError
                persona, code = _journal_actor(status, actor)
                self._journal(
                    proposal_hash,
                    persona=persona,
                    choice=Choice(str(decision)),
                    code=code,
                    text=reason or _outcome_plain(status, actor),
                    now=now,
                    payload={
                        "approval_id": approval_id,
                        "by": actor,
                        **({"env": self.settings.env.value} if actor == AUTO_APPROVER else {}),
                    },
                )
                if status is RequestStatus.APPROVED:
                    self.conn.execute(
                        """INSERT INTO routine_events (id, name, payload, created_at)
                           VALUES (?, 'approval', ?, ?)""",
                        (
                            f"evt-{uuid.uuid4().hex[:16]}",
                            json.dumps(
                                {"proposal_hash": proposal_hash, "approval_id": approval_id}
                            ),
                            to_db(now),
                        ),
                    )
        except (_LostRaceError, sqlite3.IntegrityError):
            current = self._request(proposal_hash)
            return DecideResult(
                Outcome.ALREADY_DECIDED,
                proposal_hash,
                "Already decided.",
                current.status if current else None,
            )

        req = self._request(proposal_hash)
        assert req is not None
        event = {
            RequestStatus.APPROVED: "approvals.approved",
            RequestStatus.REJECTED: "approvals.rejected",
            RequestStatus.EXPIRED: "approvals.expired",
        }[status]
        log.info(
            event,
            proposal_hash=proposal_hash,
            ticker=req.ticker,
            by=actor,
            reason=reason or None,
            approval_id=approval_id,
        )
        self._update_card(req, status, actor, now)
        outcome = {
            RequestStatus.APPROVED: Outcome.APPROVED,
            RequestStatus.REJECTED: Outcome.REJECTED,
            RequestStatus.EXPIRED: Outcome.EXPIRED,
        }[status]
        return DecideResult(
            outcome, proposal_hash, _outcome_text(status, actor, self.settings.env.value), status
        )

    # -- optional reject reason (D22) ----------------------------------------

    def record_reason(
        self, proposal_hash: str, *, user: str, text: str, now: _dt.datetime
    ) -> ReasonResult:
        """Journal an owner's optional reason for a Reject that is already recorded.

        The rejection itself was written on the click; this only adds a follow-up
        ``owner_reject`` decision (superseding the click's record) and fills the
        request's reason. A blank reason is accepted and changes nothing.
        """
        now = require_aware(now, "now").astimezone(ET)
        req = self._request(proposal_hash)
        if req is None:
            return ReasonResult("unknown_proposal", proposal_hash, "Unknown proposal.")
        if not user or user not in self.approvers:
            log.warning("approvals.reason_unauthorized", proposal_hash=proposal_hash, user=user)
            return ReasonResult(
                "unauthorized", proposal_hash, f"<@{user}> is not an allowed approver."
            )
        if req.status is not RequestStatus.REJECTED:
            return ReasonResult(
                "not_rejected", proposal_hash, f"Request is {req.status}, not rejected."
            )
        text = text.strip()
        if not text:
            return ReasonResult("blank", proposal_hash, "No reason given; the rejection stands.")
        prior = self.conn.execute(
            """SELECT id FROM decisions WHERE proposal_hash = ? AND stage = 'approval'
               AND choice = 'rejected' ORDER BY at DESC, rowid DESC LIMIT 1""",
            (proposal_hash,),
        ).fetchone()
        with self.conn:
            dec_id = self._journal(
                proposal_hash,
                persona=JournalPersona.OWNER,
                choice=Choice.REJECTED,
                code=ReasonCode.OWNER_REJECT,
                text=text,
                now=now,
                payload={"by": user, "follow_up": True},
                supersedes_id=prior["id"] if prior else None,
            )
            self.conn.execute(
                """UPDATE approval_requests SET reason = ?
                   WHERE proposal_hash = ? AND reason IN ('', ?)""",
                (f"rejected by <@{user}>: {text}", proposal_hash, f"rejected by <@{user}>"),
            )
        log.info("approvals.reject_reason", proposal_hash=proposal_hash, by=user, reason=text)
        self._update_card(req, RequestStatus.REJECTED, user, now, reason=text)
        return ReasonResult("recorded", proposal_hash, "Reason recorded.", dec_id)

    def _kind(self, proposal_hash: str) -> str:
        row = self.conn.execute(
            "SELECT kind FROM proposals WHERE proposal_hash = ?", (proposal_hash,)
        ).fetchone()
        return str(row["kind"]) if row else "open"

    def _update_card(
        self,
        req: _Request,
        status: RequestStatus,
        actor: str,
        now: _dt.datetime,
        *,
        reason: str = "",
    ) -> None:
        if req.channel == "log" or not req.message_ts:
            return
        outcome = _outcome_text(status, actor, self.settings.env.value)
        if reason:
            outcome += f" — _{reason}_"
        view = render_resolved(
            req.proposal,
            self._decision_for(req.proposal_hash),
            proposal_hash=req.proposal_hash,
            outcome=outcome,
            at=now,
            trail=load_trail(self.conn, req.proposal_hash, req.ticker),
            kind=self._kind(req.proposal_hash),
        )
        try:
            self.poster.update(req.channel, req.message_ts, view)
        except Exception as exc:  # noqa: BLE001 - the decision is committed
            log.error("approvals.update_failed", proposal_hash=req.proposal_hash, error=str(exc))


class _LostRaceError(Exception):
    pass


def _journal_actor(status: RequestStatus, actor: str) -> tuple[JournalPersona, ReasonCode]:
    if status is RequestStatus.EXPIRED:
        return JournalPersona.SYSTEM, ReasonCode.TTL_EXPIRED
    if actor == AUTO_APPROVER:
        return JournalPersona.SYSTEM, ReasonCode.AUTO_APPROVE
    if status is RequestStatus.APPROVED:
        return JournalPersona.OWNER, ReasonCode.OWNER_APPROVE
    return JournalPersona.OWNER, ReasonCode.OWNER_REJECT


def _gated_note(ready: AutoApproveReadiness) -> str:
    """Card line under the buttons when the scorecard gate held auto-approve back."""
    return f"Auto-approve held back (scorecard gate): {ready.summary()}. Approve manually."


def _not_actionable_code(reason: str) -> ReasonCode:
    if reason.startswith("gate failed"):
        return ReasonCode.NOT_ACTIONABLE_GATE_FAIL
    if reason.startswith("no gate token"):
        return ReasonCode.NOT_ACTIONABLE_NO_TOKEN
    if reason.startswith("proposal expired"):
        return ReasonCode.NOT_ACTIONABLE_EXPIRED
    return ReasonCode.NOT_ACTIONABLE_NO_GATE


def _outcome_plain(status: RequestStatus, actor: str) -> str:
    return f"{status} by {actor}"


def _ttl_reason(req: _Request) -> str:
    return f"TTL expired at {req.expires_at.astimezone(ET):%H:%M} ET with no decision"


def _who(actor: str) -> str:
    return actor if actor.startswith("arc:") else f"<@{actor}>"


def _outcome_text(status: RequestStatus, actor: str, env: str | None = None) -> str:
    if status is RequestStatus.APPROVED and actor == AUTO_APPROVER and env:
        return f":white_check_mark: *Auto-approved ({'LIVE' if env == 'live' else env})*"
    if status is RequestStatus.APPROVED:
        return f":white_check_mark: *Approved* by {_who(actor)}"
    if status is RequestStatus.REJECTED:
        return f":x: *Rejected* by {_who(actor)}"
    return ":hourglass: *Expired* — no decision within the TTL (treated as rejected)"
