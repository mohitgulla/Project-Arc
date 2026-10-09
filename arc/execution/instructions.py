"""Broker instructions that are not orders (E11.4, D73): do-not-exercise.

A DNE instruction is a broker write outside :func:`arc.execution.submit` (it
places no order and moves no money; it tells Alpaca not to auto-exercise a long
contract at expiry). It is therefore triple-guarded here:

1. ``settings.env`` is paper (D73: paper only in this card),
2. *now* is on the contract's expiration day, between the expiry-guard cutoff
   and cutoff + 10 minutes (Alpaca accepts DNE only on the expiry day, and only
   until its own cutoff),
3. the leg is held long by an open structure (the caller passes that fact),

and never under ``--dry-run``. Every call is journaled (``exit:dne``), the
broker's rejection included.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from arc.config import ArcEnv
from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import JournalStore
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings

__all__ = ["DNE_WINDOW", "DneOutcome", "do_not_exercise"]

log = structlog.get_logger(__name__)

DNE_WINDOW = _dt.timedelta(minutes=10)


class DneOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    occ: str
    sent: bool
    reason: str
    http_status: int | None = None
    at: _dt.datetime


def _refusal(
    *,
    settings: ArcSettings,
    now: _dt.datetime,
    expiry_day: _dt.date,
    cutoff: _dt.datetime,
    held_long: bool,
    dry_run: bool,
    broker: BrokerAdapter | None,
) -> str | None:
    if settings.env is not ArcEnv.PAPER:
        return f"refused: ARC_ENV={settings.env} (DNE is paper only, D73)"
    if dry_run:
        return "dry run: not sent"
    et = now.astimezone(ET)
    if et.date() != expiry_day:
        return f"refused: not the expiration day ({expiry_day})"
    if not cutoff <= et < cutoff + DNE_WINDOW:
        return f"refused: outside {cutoff:%H:%M}-{cutoff + DNE_WINDOW:%H:%M} ET"
    if not held_long:
        return "refused: not a long leg held by an open structure"
    if broker is None or not callable(getattr(broker, "do_not_exercise", None)):
        return "refused: the broker adapter has no do_not_exercise"
    return None


def do_not_exercise(
    conn: sqlite3.Connection,
    broker: BrokerAdapter | None,
    *,
    occ: str,
    ticker: str,
    structure_id: str,
    settings: ArcSettings,
    now: _dt.datetime,
    expiry_day: _dt.date,
    cutoff: _dt.datetime,
    held_long: bool,
    dry_run: bool,
    run_id: str | None = None,
    detail: str = "",
) -> DneOutcome:
    """Send Alpaca's do-not-exercise for *occ* when every guard holds; journal it either way."""
    why = _refusal(
        settings=settings, now=now, expiry_day=expiry_day, cutoff=cutoff,
        held_long=held_long, dry_run=dry_run, broker=broker,
    )  # fmt: skip
    status: int | None = None
    sent = False
    if why is None:
        try:
            broker.do_not_exercise(occ)  # type: ignore[union-attr]
            sent, why = True, "sent"
        except Exception as exc:  # noqa: BLE001 - journaled, never raised into the chain
            status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(
                exc, "status_code", None
            )
            why = f"broker rejected: {type(exc).__name__}: {exc}"[:500]
    out = DneOutcome(occ=occ, sent=sent, reason=why, http_status=status, at=now)
    with conn:
        JournalStore(conn).record(
            persona=JournalPersona.BROKER,
            stage=Stage.EXIT,
            subject=ticker,
            choice=Choice.SELECTED if sent else Choice.NOTED,
            reason_code=ReasonCode.EXIT_DNE,
            reason_text=f"do-not-exercise {occ}: {why}" + (f" ({detail})" if detail else ""),
            payload={"structure_id": structure_id, **out.model_dump(mode="json")},
            at=now,
            run_id=run_id,
        )
    log.info("execution.dne", occ=occ, sent=sent, reason=why)
    return out
