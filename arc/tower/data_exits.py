"""E13.14 (D56): the exit path on the Positions page (read-only projections).

Per open structure, the latest:

* **exit watch** — Research's ``exit_watchlist`` item (``hold | review``, thesis
  status, evidence, reason; E13.17);
* **exit case** — Quant's ``exit_case`` (subject = structure id: triggers, remaining
  EV hold / managed, close-now net, stop state, recommendation; E13.17);
* **exit review** — Risk's verdict from ``risk_exit_review`` (``close | hold``; E13.18);
* **mandatory signal** — a stop / DTE exit / expiry signal on the latest
  ``position_review`` (:data:`~arc.positions.exit_case.MANDATORY_KINDS`).

Plus the **Exit path strip** (mode · mandatory signals pending · cases today · closes
proposed today · holds today). "Latest" is by ``valid_from`` (the entry's as-of), then
``created_at``. The Tower never recomputes a case or verdict; it only reads them.

E13.15: the exit path is always ``research`` (the ``personas.exit_path`` switch was
removed); ``deterministic`` / ``shadow`` remain readable modes for fixtures.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arc.context.ttl import to_db
from arc.positions.exit_case import MANDATORY_KINDS
from arc.tower.data import _has_table, parse_ts
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

__all__ = [
    "ExitCaseView",
    "ExitPathMode",
    "ExitPathStrip",
    "ExitPathViews",
    "ExitVerdictView",
    "ExitWatchView",
    "load_exit_path",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)

ExitPathMode = Literal["deterministic", "shadow", "research"]
_MANDATORY = frozenset(k.value for k in MANDATORY_KINDS)


class ExitWatchView(BaseModel):
    """Research's latest exit-watch item for one structure."""

    model_config = _STRICT

    as_of: str
    action: Literal["hold", "review"]
    thesis_status: Literal["intact", "weakened", "broken"]
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""


class ExitCaseView(BaseModel):
    """Quant's latest exit case for one structure ($ per structure unit)."""

    model_config = _STRICT

    as_of: str
    triggers: list[str] = Field(description="'<kind>: <detail>' per trigger, in case order")
    recommendation: Literal["hold", "close"]
    remaining_ev_hold: float | None = None
    remaining_ev_managed: float | None = None
    close_now_net: float
    stop_state: str
    swap_ticker: str | None = Field(None, description="Swap pairing's open ticker, if any")
    rationale: str = ""


class ExitVerdictView(BaseModel):
    """Risk's latest exit verdict for one structure."""

    model_config = _STRICT

    as_of: str
    verdict: Literal["close", "hold"]
    reason_code: str
    reason: str
    unavailable: bool = Field(False, description="The Risk call failed: a fail-closed hold")


class ExitPathStrip(BaseModel):
    """The Positions page's Exit path strip (today = the ET day of the read)."""

    model_config = _STRICT

    mode: ExitPathMode = Field(description="Exit path mode (research since the D56 cutover)")
    mandatory_pending: int = Field(0, ge=0, description="Open positions with a mandatory signal")
    cases_today: int = Field(0, ge=0, description="exit_case entries written today")
    closes_proposed_today: int = Field(0, ge=0, description="Close proposals created today")
    holds_today: int = Field(0, ge=0, description="Risk exit verdicts `hold` today")


class ExitPathViews(BaseModel):
    """One structure's exit-path fields (each ``None`` when nothing was written)."""

    model_config = _STRICT

    exit_watch: ExitWatchView | None = None
    exit_case: ExitCaseView | None = None
    exit_review: ExitVerdictView | None = None
    mandatory_signal: str | None = None


def _rows(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    """Every entry of *kind*, newest first."""
    return conn.execute(
        "SELECT subject, payload, valid_from, created_at FROM context_entries WHERE kind = ?"
        " ORDER BY valid_from DESC, created_at DESC, rowid DESC",
        (kind,),
    ).fetchall()


def _payload(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        out = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _as_of(row: sqlite3.Row, payload: dict[str, Any]) -> str:
    stamp = parse_ts(row["valid_from"])
    return stamp.isoformat() if stamp is not None else str(payload.get("as_of") or "")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _watch(conn: sqlite3.Connection, ids: set[str]) -> dict[str, ExitWatchView]:
    out: dict[str, ExitWatchView] = {}
    for r in _rows(conn, "exit_watchlist"):
        p = _payload(r)
        if p is None:
            continue
        for item in p.get("items") or []:
            sid = item.get("structure_id") if isinstance(item, dict) else None
            if sid not in ids or sid in out:
                continue
            try:
                out[sid] = ExitWatchView.model_validate(
                    {
                        "as_of": _as_of(r, p),
                        "action": item.get("action"),
                        "thesis_status": item.get("thesis_status"),
                        "evidence": [str(e) for e in item.get("evidence") or []],
                        "reason": str(item.get("reason") or ""),
                    }
                )
            except ValidationError:
                continue
        if len(out) == len(ids):
            break
    return out


def _cases(conn: sqlite3.Connection, ids: set[str]) -> dict[str, ExitCaseView]:
    out: dict[str, ExitCaseView] = {}
    for r in _rows(conn, "exit_case"):
        sid = r["subject"]
        if sid not in ids or sid in out:
            continue
        p = _payload(r)
        if p is None:
            continue
        raw_facts, raw_swap = p.get("facts"), p.get("swap")
        facts: dict[str, Any] = raw_facts if isinstance(raw_facts, dict) else {}
        swap: dict[str, Any] = raw_swap if isinstance(raw_swap, dict) else {}
        triggers = [
            f"{t.get('kind')}: {t.get('detail')}" if t.get("detail") else str(t.get("kind"))
            for t in p.get("triggers") or []
            if isinstance(t, dict)
        ]
        try:
            out[sid] = ExitCaseView.model_validate(
                {
                    "as_of": _as_of(r, p),
                    "triggers": triggers,
                    "recommendation": p.get("recommendation"),
                    "remaining_ev_hold": _num(facts.get("remaining_ev_hold")),
                    "remaining_ev_managed": _num(facts.get("remaining_ev_managed")),
                    "close_now_net": _num(facts.get("close_now_net")) or 0.0,
                    "stop_state": str(facts.get("stop_state") or "off"),
                    "swap_ticker": str(swap["open_ticker"]) if swap.get("open_ticker") else None,
                    "rationale": str(p.get("rationale") or ""),
                }
            )
        except ValidationError:
            continue
    return out


def _verdicts(conn: sqlite3.Connection, ids: set[str]) -> dict[str, ExitVerdictView]:
    out: dict[str, ExitVerdictView] = {}
    for r in _rows(conn, "risk_exit_review"):
        p = _payload(r)
        if p is None:
            continue
        for v in p.get("verdicts") or []:
            sid = v.get("structure_id") if isinstance(v, dict) else None
            if sid not in ids or sid in out:
                continue
            try:
                out[sid] = ExitVerdictView.model_validate(
                    {
                        "as_of": _as_of(r, p),
                        "verdict": v.get("verdict"),
                        "reason_code": str(v.get("reason_code") or ""),
                        "reason": str(v.get("reason") or ""),
                        "unavailable": bool(p.get("unavailable")),
                    }
                )
            except ValidationError:
                continue
        if len(out) == len(ids):
            break
    return out


def _mandatory(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    """Structure id -> the first mandatory signal kind on its latest ``position_review``."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    for r in _rows(conn, "position_review"):
        sid = r["subject"]
        if sid not in ids or sid in seen:
            continue
        seen.add(sid)
        p = _payload(r) or {}
        kinds = [str(s.get("kind")) for s in p.get("signals") or [] if isinstance(s, dict)]
        hit = next((k for k in kinds if k in _MANDATORY), None)
        if hit is not None:
            out[sid] = hit
    return out


def _today_bounds(now: _dt.datetime) -> tuple[str, str]:
    day = now.astimezone(ET).date()
    lo = _dt.datetime.combine(day, _dt.time.min, tzinfo=ET)
    return to_db(lo), to_db(lo + _dt.timedelta(days=1))


def _strip(
    conn: sqlite3.Connection, mode: ExitPathMode, now: _dt.datetime, mandatory: dict[str, str]
) -> ExitPathStrip:
    lo, hi = _today_bounds(now)
    cases = conn.execute(
        "SELECT COUNT(*) FROM context_entries WHERE kind = 'exit_case'"
        " AND valid_from >= ? AND valid_from < ?",
        (lo, hi),
    ).fetchone()[0]
    holds = 0
    for r in conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'risk_exit_review'"
        " AND valid_from >= ? AND valid_from < ?",
        (lo, hi),
    ):
        p = _payload(r) or {}
        holds += sum(
            1 for v in p.get("verdicts") or [] if isinstance(v, dict) and v.get("verdict") == "hold"
        )
    closes = 0
    if _has_table(conn, "proposals"):
        day_lo = now.astimezone(ET).date()
        for r in conn.execute("SELECT created_at FROM proposals WHERE kind = 'close'"):
            at = parse_ts(r["created_at"])
            if at is not None and at.astimezone(ET).date() == day_lo:
                closes += 1
    return ExitPathStrip(
        mode=mode,
        mandatory_pending=len(mandatory),
        cases_today=int(cases),
        closes_proposed_today=closes,
        holds_today=holds,
    )


def load_exit_path(
    conn: sqlite3.Connection,
    open_ids: Iterable[str],
    *,
    mode: ExitPathMode,
    now: _dt.datetime,
) -> tuple[ExitPathStrip, dict[str, ExitPathViews]]:
    """The strip and each open structure's exit-path views (SELECT only).

    ``deterministic`` short-circuits: the strip has the mode only, no views.
    """
    if mode == "deterministic" or not _has_table(conn, "context_entries"):
        return ExitPathStrip.model_validate({"mode": mode}), {}
    ids = set(open_ids)
    mandatory = _mandatory(conn, ids)
    strip = _strip(conn, mode, now, mandatory)
    if not ids:
        return strip, {}
    watch, cases, verdicts = _watch(conn, ids), _cases(conn, ids), _verdicts(conn, ids)
    views = {
        sid: ExitPathViews(
            exit_watch=watch.get(sid),
            exit_case=cases.get(sid),
            exit_review=verdicts.get(sid),
            mandatory_signal=mandatory.get(sid),
        )
        for sid in ids
    }
    return strip, views
