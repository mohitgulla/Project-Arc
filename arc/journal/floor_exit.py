"""Remaining-EV floor exit facts (E6.4a observability). Pure reads of the audit store.

For a position the E6.4 manager closed on the D19 remaining-EV floor, a reviewer
needs, without reading payloads: remaining EV per $ BP, the floor, the open's
managed Net EV per $ BP, minutes since the fill, and which evaluation window
(end-of-day vs intraday marks) fired. :func:`floor_exit_facts` assembles them from
the ``exit:remaining_ev_floor`` journal row (its ``review`` payload is the
``position_review`` the close was proposed from), the position and the open's
frozen market context. Reviews written before E6.4a lack the window fields; those
are derived from the stored detail text / timestamps where possible, else ``None``.

Used by ``arc journal explain`` and the tower trade detail. No LLM, no network.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from arc.journal.reasons import ReasonCode
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

__all__ = ["FloorExitFacts", "floor_exit_facts", "floor_exit_line"]

_FLOOR_RE = re.compile(r"< floor ([+-]?\d+(?:\.\d+)?)")


class FloorExitFacts(BaseModel):
    """Why a position was closed on the remaining-EV floor (all money $ per unit)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    structure_id: str | None = None
    exit_proposal_hash: str
    open_proposal_hash: str | None = None
    decided_at: _dt.datetime | None = None
    remaining_ev: float | None = None
    remaining_ev_per_bp: float | None = None
    floor: float | None = None
    entry_managed_net_ev: float | None = None
    entry_managed_net_ev_per_bp: float | None = None
    buying_power: float | None = None
    minutes_since_fill: float | None = Field(None, ge=0.0)
    days_held: int | None = Field(None, ge=0, description="ET calendar days fill -> decision")
    window: str | None = Field(
        None, description="'end_of_day' or 'intraday' marks fired it; None = not recorded"
    )
    floor_mode: str | None = Field(
        None, description="Floor config when it fired: 'eod' (EOD only) or 'intraday'"
    )


def _ts(text: Any) -> _dt.datetime | None:
    if not text:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return ts.replace(tzinfo=_dt.UTC) if ts.tzinfo is None else ts


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _entry_ev(conn: sqlite3.Connection, phash: str | None) -> float | None:
    if not phash:
        return None
    row = conn.execute(
        """SELECT payload FROM market_contexts WHERE proposal_hash = ?
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (phash,),
    ).fetchone()
    if row is None:
        return None
    em = (json.loads(row[0]).get("analytics") or {}).get("exit_model") or {}
    return _f((em.get("managed") or {}).get("net_ev"))


def floor_exit_facts(conn: sqlite3.Connection, phash: str) -> FloorExitFacts | None:
    """Floor-exit facts for *phash* (the close proposal, or the open it closed)."""
    pos = conn.execute(
        """SELECT id, open_proposal_hash, exit_proposal_hash, opened_at FROM open_structures
           WHERE open_proposal_hash = ? OR exit_proposal_hash = ?
           ORDER BY opened_at LIMIT 1""",
        (phash, phash),
    ).fetchone()
    hashes = {phash}
    if pos is not None and pos["exit_proposal_hash"]:
        hashes.add(pos["exit_proposal_hash"])
    marks = ",".join("?" * len(hashes))
    row = conn.execute(
        f"""SELECT proposal_hash, payload, at FROM decisions
            WHERE reason_code = ? AND proposal_hash IN ({marks})
            ORDER BY at DESC, rowid DESC LIMIT 1""",  # noqa: S608 - placeholders only
        (ReasonCode.EXIT_EV_FLOOR.value, *sorted(hashes)),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"] or "{}")
    rv: dict[str, Any] = payload.get("review") or {}
    at = _ts(row["at"])
    opened = _ts(pos["opened_at"]) if pos is not None else None
    detail = " ".join(str(s.get("detail", "")) for s in rv.get("signals") or [])
    floor = _f(rv.get("ev_floor"))
    if floor is None and (m := _FLOOR_RE.search(detail)):
        floor = float(m.group(1))
    bp = _f(rv.get("buying_power"))
    open_hash = pos["open_proposal_hash"] if pos is not None else None
    entry = _f(rv.get("entry_managed_net_ev"))
    if entry is None:
        entry = _entry_ev(conn, open_hash)
    entry_bp = _f(rv.get("entry_managed_net_ev_per_bp"))
    if entry_bp is None and entry is not None and bp:
        entry_bp = round(entry / bp, 6)
    minutes = _f(rv.get("minutes_since_fill"))
    if minutes is None and at is not None and opened is not None:
        minutes = round(max((at - opened).total_seconds() / 60.0, 0.0), 1)
    days = None
    if at is not None and opened is not None:
        days = max((at.astimezone(ET).date() - opened.astimezone(ET).date()).days, 0)
    eod = rv.get("end_of_day")
    window = None if eod is None else ("end_of_day" if eod else "intraday")
    return FloorExitFacts(
        structure_id=pos["id"] if pos is not None else payload.get("structure_id"),
        exit_proposal_hash=row["proposal_hash"],
        open_proposal_hash=open_hash,
        decided_at=at,
        remaining_ev=_f(rv.get("remaining_ev")),
        remaining_ev_per_bp=_f(rv.get("remaining_ev_per_bp")),
        floor=floor,
        entry_managed_net_ev=entry,
        entry_managed_net_ev_per_bp=entry_bp,
        buying_power=bp,
        minutes_since_fill=minutes,
        days_held=days,
        window=window,
        floor_mode=rv.get("ev_floor_window"),
    )


def _num(v: float | None, fmt: str) -> str:
    return "n/a" if v is None else format(v, fmt)


def floor_exit_line(f: FloorExitFacts) -> str:
    """One plain line for ``arc journal explain``."""
    window = {"end_of_day": "end-of-day marks", "intraday": "intraday marks"}.get(
        f.window or "", "window not recorded (pre-E6.4a)"
    )
    return (
        f"remaining-EV floor exit: remaining EV/$BP {_num(f.remaining_ev_per_bp, '+.4f')}"
        f" vs floor {_num(f.floor, '+.4f')} · entry managed Net EV/$BP "
        f"{_num(f.entry_managed_net_ev_per_bp, '+.4f')}"
        f" ({'n/a' if f.entry_managed_net_ev is None else f'${f.entry_managed_net_ev:+,.2f}'}"
        f"/unit) · {_num(f.minutes_since_fill, '.0f')} min since fill"
        f" · days held {f.days_held if f.days_held is not None else 'n/a'} · fired on {window}"
    )
