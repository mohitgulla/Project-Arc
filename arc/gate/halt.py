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
- E11.4 (D73): a halt has a *scope*. ``all`` (every halt before D73) stops opens
  and closes; ``opens`` (actor ``arc:expiry``) stops new opens only, so exits keep
  running. ``HaltState.halted`` is True only for a scope-``all`` halt (or a read
  error); ``HaltState.opens_only`` is True when every active halt is scope
  ``opens``. Only the owner clears either.
- E3.9 (D122): the daily-loss auto-halt is scope ``opens`` (exits keep running) and
  fires only after ``daily_loss_confirm_ticks`` consecutive ``monitor`` breaches. The
  count is kept by the caller (:class:`DailyLossTicks`, persisted by ``monitor``); the
  gate's per-snapshot ``daily_loss`` rule still blocks opens on any single breach.
- E3.9a (D137): a ``daily_loss`` halt lasts for its own ET session only. The routines
  tick (and ``monitor``) clear it at the first session after it with
  :meth:`HaltSwitch.expire_stale_session_halts` (``cleared_by = arc:session-rollover``),
  and a switch built with ``now`` already ignores it on any later calendar day
  (:func:`daily_loss_expired`). Every other halt kind still needs the owner's resume.

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
from arc.utils.calendar import ET, is_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.config import ArcSettings
    from arc.gate.band import PriceBand
    from arc.gate.inputs import AccountSnapshot, MarketSnapshot, Portfolio
    from arc.models import GateDecision, Proposal
    from arc.store.repos import HaltRepo

log = structlog.get_logger(__name__)

__all__ = [
    "DailyLossTicks",
    "HaltKind",
    "HaltRecord",
    "HaltScope",
    "HaltSwitch",
    "HaltState",
    "ResumeNotAuthorizedError",
    "SESSION_ROLLOVER_ACTOR",
    "daily_loss_breach",
    "daily_loss_expired",
    "daily_loss_halt_reason",
    "evaluate_with_halt",
    "halt_row_expired",
    "session_rollover_notice",
]


class HaltKind(StrEnum):
    MANUAL = "manual"
    DAILY_LOSS = "daily_loss"


class HaltScope(StrEnum):
    """E11.4 (D73): what a halt stops."""

    ALL = "all"  # opens and closes
    OPENS = "opens"  # new opens only; exits keep running


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
    scope: HaltScope = HaltScope.ALL


class HaltState(BaseModel):
    """Active halts at read time.

    ``halted`` is True on any active scope-``all`` halt or a read error (opens and
    closes stop). ``opens_only`` is True when halts are active and every one is
    scope ``opens`` (new opens stop, exits run). ``opens_blocked`` = either.
    """

    model_config = ConfigDict(frozen=True)

    halted: bool
    active: list[HaltRecord]
    error: str | None = None
    opens_only: bool = False

    @property
    def opens_blocked(self) -> bool:
        return self.halted or self.opens_only


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
        scope=HaltScope(str(row.get("scope") or HaltScope.ALL.value)),
    )


def daily_loss_breach(account: AccountSnapshot, config: ArcSettings) -> str | None:
    """Pure: the daily-loss violation detail if the halt threshold is reached, else None.

    Uses the gate's own ``check_daily_loss`` so the auto-halt and the gate rule can
    never disagree about the threshold (>= ``daily_loss_halt_pct``, fails closed on
    a non-positive start-of-day equity).
    """
    violations = check_daily_loss(account, config)
    return violations[0].detail if violations else None


def daily_loss_expired(halt: HaltRecord, now: dt.datetime) -> bool:
    """Pure (E3.9a, D137): True when *halt* is a ``daily_loss`` halt from an earlier ET day.

    A daily-loss halt stops opens for its own session only. Any later calendar day is
    past that session (a weekend or holiday has no opens to stop), so the read side
    ignores it from midnight ET; the audited clear waits for the next trading session.
    Other kinds never expire. A row without a ``session_date`` never expires (fail closed).
    """
    return halt_row_expired(halt.kind.value, halt.session_date, now)


def halt_row_expired(kind: str, session_date: dt.date | str | None, now: dt.datetime) -> bool:
    """Pure (E3.9a, D137): :func:`daily_loss_expired` on raw ``halts`` columns (Tower)."""
    if kind != HaltKind.DAILY_LOSS.value or not session_date:
        return False
    if isinstance(session_date, str):
        session_date = dt.date.fromisoformat(session_date)
    return session_date < now.astimezone(ET).date()


#: E3.9a (D137): ``cleared_by`` of a daily-loss halt cleared at the next session.
SESSION_ROLLOVER_ACTOR = "arc:session-rollover"


def session_rollover_notice(halt: HaltRecord) -> str:
    """Pure (E3.9a): the #arc-investor day-thread line for a halt the rollover cleared."""
    day = halt.session_date or halt.at.date()
    return (
        f"[Ops] daily-loss halt from {day:%a %b} {day.day} expired at session open; opens allowed"
    )


def daily_loss_halt_reason(account: AccountSnapshot, config: ArcSettings, ticks: int) -> str:
    """Pure (E3.9): the stored halt reason, e.g. ``day loss 3.01% >= 3.00% on 2 ticks``.

    A non-positive start-of-day equity (the rule fails closed) keeps the gate's detail.
    """
    plural = "" if ticks == 1 else "s"
    if account.last_equity <= 0:
        return f"{daily_loss_breach(account, config)} on {ticks} tick{plural}"
    loss = (account.last_equity - account.equity) / account.last_equity
    return f"day loss {loss:.2%} >= {config.daily_loss_halt_pct:.2%} on {ticks} tick{plural}"


class DailyLossTicks(BaseModel):
    """E3.9 (D122): consecutive ``monitor`` ticks at or over the daily-loss threshold.

    Pure value: :meth:`advance` returns the next count. A non-breach tick resets it to
    0, and a new ET session starts from 0. The caller persists it (``routine_state``
    key ``daily_loss_breach:<session>``); the gate never stores anything.
    """

    model_config = ConfigDict(frozen=True)

    session: dt.date
    count: int = 0
    last_at: dt.datetime | None = None

    def advance(self, *, breached: bool, now: dt.datetime) -> DailyLossTicks:
        session = now.astimezone(ET).date()
        prior = self.count if session == self.session else 0
        return DailyLossTicks(session=session, count=prior + 1 if breached else 0, last_at=now)


class HaltSwitch:
    """Store-backed kill switch over a :class:`~arc.store.repos.HaltRepo`; holds no state itself.

    Takes the repo, not a connection, so nothing in ``arc.gate`` touches sqlite3 directly.

    E3.9a (D137): with *now* (a clock or a fixed time) the read side ignores
    ``daily_loss`` halts from an earlier ET day (:func:`daily_loss_expired`). Without it
    every uncleared row counts, as before.
    """

    def __init__(
        self,
        repo: HaltRepo,
        *,
        now: dt.datetime | Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._now = now

    def _read_now(self) -> dt.datetime | None:
        if self._now is None or isinstance(self._now, dt.datetime):
            return self._now
        return self._now()

    # -- read ------------------------------------------------------------

    def state(self) -> HaltState:
        """Active halts; fails closed (halted) if the store cannot be read or parsed."""
        try:
            active = [_record(r) for r in self._repo.active()]
            now = self._read_now()
            if now is not None:
                active = [h for h in active if not daily_loss_expired(h, now)]
        except Exception as exc:  # noqa: BLE001 — any read/parse error means halted
            log.error("halt.state_unreadable", error=str(exc))
            return HaltState(halted=True, active=[], error=f"{type(exc).__name__}: {exc}")
        full = any(h.scope is HaltScope.ALL for h in active)
        return HaltState(halted=full, active=active, opens_only=bool(active) and not full)

    def is_halted(self) -> bool:
        """True while a scope-``all`` halt is active (opens *and* closes stop)."""
        return self.state().halted

    def opens_blocked(self) -> bool:
        """True while any halt is active: new opens stop (E11.4: also opens-only halts)."""
        return self.state().opens_blocked

    def apply(self, account: AccountSnapshot) -> AccountSnapshot:
        """Return ``account`` with ``halted`` / ``opens_halted`` set from the store.

        Never clears a flag the caller already set.
        """
        state = self.state()
        update: dict[str, bool] = {}
        if state.halted and not account.halted:
            update["halted"] = True
        if state.opens_only and not account.opens_halted:
            update["opens_halted"] = True
        return account.model_copy(update=update) if update else account

    # -- write -----------------------------------------------------------

    def halt(
        self,
        *,
        actor: str,
        reason: str,
        now: dt.datetime,
        kind: HaltKind = HaltKind.MANUAL,
        run_id: str | None = None,
        scope: HaltScope = HaltScope.ALL,
    ) -> HaltRecord:
        """Raise a halt immediately. Anyone may halt; repeated halts stack (all must clear).

        ``scope=HaltScope.OPENS`` (E11.4, D73) stops new opens only.
        """
        at = _to_store(now)
        session = now.astimezone(ET).date()
        halt_id = self._repo.halt(
            reason=reason,
            actor=actor,
            kind=kind.value,
            session_date=session.isoformat(),
            at=at,
            run_id=run_id,
            scope=scope.value,
        )
        return HaltRecord(
            id=halt_id,
            kind=kind,
            reason=reason,
            actor=actor,
            at=now.astimezone(ET),
            session_date=session,
            scope=scope,
        )

    def halt_opens_once(
        self, *, actor: str, reason: str, now: dt.datetime, run_id: str | None = None
    ) -> HaltRecord | None:
        """E11.4 (D73): raise an opens-only halt at most once per ET session per
        ``(actor, reason)`` (an owner ``!resume`` is not undone by the next tick).
        Returns the new halt, or ``None`` when this session already had it.
        """
        session = now.astimezone(ET).date().isoformat()
        if self._repo.exists_for_reason(actor=actor, reason=reason, session_date=session):
            return None
        return self.halt(actor=actor, reason=reason, now=now, run_id=run_id, scope=HaltScope.OPENS)

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

    def expire_stale_session_halts(self, now: dt.datetime) -> list[HaltRecord]:
        """E3.9a (D137): clear ``daily_loss`` halts whose session has ended; audited.

        Runs only on a trading day (ET): a Friday halt is cleared by the first tick of
        Monday's session (or Tuesday's after a holiday Monday), never on the weekend.
        Each cleared row gets ``cleared_at = now`` and ``cleared_by =``
        :data:`SESSION_ROLLOVER_ACTOR`. Other kinds and today's halts are untouched.
        Idempotent: returns only the rows this call cleared (empty on a re-run).
        """
        today = now.astimezone(ET).date()
        if not is_session(today):
            return []
        at = _to_store(now)
        cleared: list[HaltRecord] = []
        for row in self._repo.active():
            halt = _record(row)
            if not daily_loss_expired(halt, now):
                continue
            if self._repo.resume(halt.id, actor=SESSION_ROLLOVER_ACTOR, at=at):
                log.info(
                    "halt.session_rollover_cleared",
                    halt_id=halt.id,
                    kind=halt.kind.value,
                    session_date=str(halt.session_date),
                    reason=halt.reason,
                )
                cleared.append(
                    halt.model_copy(
                        update={
                            "cleared_at": now.astimezone(ET),
                            "cleared_by": SESSION_ROLLOVER_ACTOR,
                        }
                    )
                )
        return cleared

    def check_daily_loss(
        self,
        account: AccountSnapshot,
        config: ArcSettings,
        *,
        now: dt.datetime,
        ticks: int = 1,
    ) -> HaltRecord | None:
        """Auto-halt if the daily-loss rule trips; at most once per ET trading session.

        E3.9 (D122): *ticks* is the caller's count of consecutive breached ``monitor``
        ticks, this one included (:class:`DailyLossTicks`). The halt is raised only once
        ``ticks >= daily_loss_confirm_ticks``, and its scope is ``opens``: new opens stop
        for the session, exits keep running through the normal gate.

        Returns the new halt, or None if no halt was raised (no breach, not yet
        confirmed, or this session already had a daily-loss halt — even one the owner
        cleared).
        """
        if daily_loss_breach(account, config) is None:
            return None
        if ticks < config.daily_loss_confirm_ticks:
            return None
        session = now.astimezone(ET).date().isoformat()
        if self._repo.exists_for_session(kind=HaltKind.DAILY_LOSS.value, session_date=session):
            return None
        return self.halt(
            actor="arc:daily-loss",
            reason=daily_loss_halt_reason(account, config, ticks),
            now=now,
            kind=HaltKind.DAILY_LOSS,
            scope=HaltScope.OPENS,
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
    close_max_steps: int | None = None,
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
        close_max_steps=close_max_steps,
    )
