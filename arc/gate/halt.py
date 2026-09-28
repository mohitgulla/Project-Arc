"""Kill switch + daily halt state (E3.3).

Halt state lives in the audit store (``halts`` table), outside the LLM loop, so
it survives process restarts. This module owns the *policy*:

- anyone may halt (``!halt`` from any thread) — halting is always safe;
- only the configured owner (``ARC_OWNER_SLACK_USER_ID``) may resume, and a
  resume clears every active halt;
- the daily-loss rule auto-halts at most once per trading session (so an owner
  ``!resume`` is not immediately undone by the same loss). The gate's
  ``daily_loss_halt`` rule still blocks new entries while the loss persists;
- reading the state fails closed: if the store cannot be read, we are halted.

The gate itself stays pure: callers stamp the halt flag onto the
:class:`~arc.gate.inputs.AccountSnapshot` with :meth:`HaltSwitch.apply` before
calling :func:`arc.gate.rules.evaluate`. No Slack or network here — posting is
done by :mod:`arc.slack.halt`.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from arc.gate.rules import check_daily_loss, evaluate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from arc.config import ArcSettings
    from arc.gate.band import PriceBand
    from arc.gate.inputs import AccountSnapshot, MarketSnapshot, Portfolio
    from arc.models import GateDecision, Proposal
    from arc.store.repos import HaltRepo

log = structlog.get_logger(__name__)

__all__ = [
    "HaltKind",
    "HaltRecord",
    "HaltSwitch",
    "HaltState",
    "ResumeNotAuthorizedError",
    "daily_loss_breach",
    "evaluate_with_halt",
]


class HaltKind(StrEnum):
    MANUAL = "manual"
    DAILY_LOSS = "daily_loss"


class HaltRecord(BaseModel):
    """One persisted halt row. Timestamps are ET-aware."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: HaltKind
    reason: str
    actor: str
    at: dt.datetime
    cleared_at: dt.datetime | None = None
    cleared_by: str | None = None
    session_date: dt.date | None = None


class HaltState(BaseModel):
    """Active halts at read time. ``halted`` is True on any active halt or a read error."""

    model_config = ConfigDict(frozen=True)

    halted: bool
    active: list[HaltRecord]
    error: str | None = None


class ResumeNotAuthorizedError(PermissionError):
    """Raised when someone other than the owner tries to resume trading."""


def _to_store(ts: dt.datetime) -> str:
    if ts.tzinfo is None or ts.utcoffset() is None:
        msg = "halt timestamps must be timezone-aware (use arc.utils.calendar.now_et())"
        raise ValueError(msg)
    return ts.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _from_store(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(ET)


def _record(row: dict[str, object]) -> HaltRecord:
    at = _from_store(str(row["at"]))
    assert at is not None  # column is NOT NULL
    session = row.get("session_date")
    return HaltRecord(
        id=str(row["id"]),
        kind=HaltKind(str(row["kind"])),
        reason=str(row["reason"]),
        actor=str(row["actor"]),
        at=at,
        cleared_at=_from_store(row.get("cleared_at")),  # type: ignore[arg-type]
        cleared_by=row.get("cleared_by"),  # type: ignore[arg-type]
        session_date=dt.date.fromisoformat(str(session)) if session else None,
    )


def daily_loss_breach(account: AccountSnapshot, config: ArcSettings) -> str | None:
    """Pure: the daily-loss violation detail if the halt threshold is reached, else None.

    Uses the gate's own ``check_daily_loss`` so the auto-halt and the gate rule can
    never disagree about the threshold (>= ``daily_loss_halt_pct``, fails closed on
    a non-positive start-of-day equity).
    """
    violations = check_daily_loss(account, config)
    return violations[0].detail if violations else None


class HaltSwitch:
    """Store-backed kill switch over a :class:`~arc.store.repos.HaltRepo`; holds no state itself.

    Takes the repo, not a connection, so nothing in ``arc.gate`` touches sqlite3 directly.
    """

    def __init__(self, repo: HaltRepo) -> None:
        self._repo = repo

    # -- read ------------------------------------------------------------

    def state(self) -> HaltState:
        """Active halts; fails closed (halted) if the store cannot be read or parsed."""
        try:
            active = [_record(r) for r in self._repo.active()]
        except Exception as exc:  # noqa: BLE001 — any read/parse error means halted
            log.error("halt.state_unreadable", error=str(exc))
            return HaltState(halted=True, active=[], error=f"{type(exc).__name__}: {exc}")
        return HaltState(halted=bool(active), active=active)

    def is_halted(self) -> bool:
        return self.state().halted

    def apply(self, account: AccountSnapshot) -> AccountSnapshot:
        """Return ``account`` with ``halted`` set from the store (never cleared by it)."""
        if account.halted or not self.is_halted():
            return account
        return account.model_copy(update={"halted": True})

    # -- write -----------------------------------------------------------

    def halt(
        self,
        *,
        actor: str,
        reason: str,
        now: dt.datetime,
        kind: HaltKind = HaltKind.MANUAL,
        run_id: str | None = None,
    ) -> HaltRecord:
        """Raise a halt immediately. Anyone may halt; repeated halts stack (all must clear)."""
        at = _to_store(now)
        session = now.astimezone(ET).date()
        halt_id = self._repo.halt(
            reason=reason,
            actor=actor,
            kind=kind.value,
            session_date=session.isoformat(),
            at=at,
            run_id=run_id,
        )
        return HaltRecord(
            id=halt_id,
            kind=kind,
            reason=reason,
            actor=actor,
            at=now.astimezone(ET),
            session_date=session,
        )

    def resume(self, *, actor: str, config: ArcSettings, now: dt.datetime) -> list[HaltRecord]:
        """Clear every active halt. Only ``config.owner_slack_user_id`` may resume.

        Returns the halts that were cleared (empty if trading was not halted).
        """
        if actor != config.owner_slack_user_id:
            log.warning("halt.resume_denied", actor=actor)
            msg = f"only the owner may resume trading (got {actor!r})"
            raise ResumeNotAuthorizedError(msg)
        active = self.state().active
        self._repo.clear_all(actor=actor, at=_to_store(now))
        return active

    def check_daily_loss(
        self, account: AccountSnapshot, config: ArcSettings, *, now: dt.datetime
    ) -> HaltRecord | None:
        """Auto-halt if the daily-loss rule trips; at most once per ET trading session.

        Returns the new halt, or None if no halt was raised (no breach, or this
        session already had a daily-loss halt — even one the owner cleared).
        """
        detail = daily_loss_breach(account, config)
        if detail is None:
            return None
        session = now.astimezone(ET).date().isoformat()
        if self._repo.exists_for_session(kind=HaltKind.DAILY_LOSS.value, session_date=session):
            return None
        return self.halt(
            actor="arc:daily-loss",
            reason=detail,
            now=now,
            kind=HaltKind.DAILY_LOSS,
        )


def evaluate_with_halt(
    switch: HaltSwitch,
    proposal: Proposal,
    account_snapshot: AccountSnapshot,
    portfolio: Portfolio,
    config: ArcSettings,
    *,
    market: MarketSnapshot,
    now: dt.datetime,
    band: PriceBand | None = None,
    closing: bool = False,
) -> GateDecision:
    """The gate as callers must run it: persisted halt state stamped in, then :func:`evaluate`."""
    return evaluate(
        proposal,
        switch.apply(account_snapshot),
        portfolio,
        config,
        market=market,
        now=now,
        band=band,
        closing=closing,
    )
