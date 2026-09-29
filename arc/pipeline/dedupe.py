"""Idea dedupe (E5.9, D33): a repeated idea never reaches Quant or propose twice.

An **idea fingerprint** is ``(ticker, stance, structure_type, expiry ISO week,
short-strike bucket)`` where the bucket is ``round(short strike / (0.01 × spot))``
(the long strike for single-leg longs). A candidate idea is *suppressed* when the
same fingerprint was recently

``executed``   an open structure exists, one closed within
               ``dedupe_executed_cooldown_sessions``, or a ladder is working
``proposed``   proposed (pending, or expired by TTL) within
               ``dedupe_proposed_cooldown_sessions``
``rejected``   rejected by the owner within ``dedupe_rejected_cooldown_sessions``

Cooldowns are trading sessions (config) and are multiplied in the D32 restrictive
tier. A **material change** re-admits the idea: spot moved at least
``dedupe_reprice_move_pct`` since the last one, or the Director's market regime
changed. Everything here is deterministic and reads only the audit DB.

Two call sites (:mod:`arc.pipeline.steps`):

* the Director stage, by ``(ticker, stance)`` prefix before the LLM call —
  suppressed names are listed to the Director as "recently suggested / held" and
  names with an open structure in the same stance are removed outright;
* propose, by the full fingerprint, as the final check.
"""

from __future__ import annotations

import datetime as _dt
import enum
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.journal.reasons import ReasonCode
from arc.models import LegIntent, Stance, Structure, StructureKind
from arc.structures import parse_occ
from arc.utils.calendar import ET, add_sessions, is_session, previous_session

if TYPE_CHECKING:
    import sqlite3

    from arc.budget.orders import Tier
    from arc.config import ArcSettings

__all__ = [
    "DedupeConfig",
    "DedupeVerdict",
    "IdeaFingerprint",
    "Suppression",
    "check_idea",
    "fingerprint",
    "recent_ideas",
    "sessions_back",
    "strike_bucket",
]

log = structlog.get_logger(__name__)

_FORBID = ConfigDict(extra="forbid")
_BUCKET_PCT = Decimal("0.01")

# Structure kinds to the Director's structure-type vocabulary (steps.STRUCTURE_TYPES).
STRUCTURE_TYPE_OF_KIND: dict[StructureKind, str] = {
    StructureKind.LONG_CALL: "long_call",
    StructureKind.LONG_PUT: "long_put",
    StructureKind.VERTICAL_DEBIT: "vertical_spread",
    StructureKind.VERTICAL_CREDIT: "vertical_spread",
    StructureKind.IRON_CONDOR: "iron_condor",
    StructureKind.OTHER: "other",
}


class Suppression(enum.StrEnum):
    """Why an idea is a repeat."""

    EXECUTED = "executed"
    PROPOSED = "proposed"
    REJECTED = "rejected"

    @property
    def reason_code(self) -> ReasonCode:
        return {
            Suppression.EXECUTED: ReasonCode.DEDUPE_EXECUTED,
            Suppression.PROPOSED: ReasonCode.DEDUPE_PROPOSED,
            Suppression.REJECTED: ReasonCode.DEDUPE_REJECTED,
        }[self]


class IdeaFingerprint(BaseModel):
    """The deterministic identity of a trade idea (D33)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    stance: Stance
    structure_type: str
    expiry_week: str = Field(..., description="ISO week of the (first) expiry, e.g. 2026-W44")
    strike_bucket: int = Field(..., description="round(short strike / (1% of spot))")

    def key(self) -> str:
        return (
            f"{self.ticker}|{self.stance.value}|{self.structure_type}|"
            f"{self.expiry_week}|{self.strike_bucket}"
        )

    def prefix(self) -> str:
        """The Director-stage key: ``ticker|stance``."""
        return f"{self.ticker}|{self.stance.value}"

    @classmethod
    def parse(cls, key: str) -> IdeaFingerprint:
        t, s, st, w, b = key.split("|")
        return cls(
            ticker=t, stance=Stance(s), structure_type=st, expiry_week=w, strike_bucket=int(b)
        )


class DedupeConfig(BaseModel):
    """Effective cooldowns (sessions) plus the material-change override."""

    model_config = _FORBID

    executed_sessions: int = Field(5, ge=0)
    proposed_sessions: int = Field(1, ge=0)
    rejected_sessions: int = Field(1, ge=0)
    reprice_move_pct: float = Field(0.03, ge=0.0, le=1.0)

    @classmethod
    def from_settings(cls, settings: ArcSettings, tier: Tier | None = None) -> DedupeConfig:
        """Base cooldowns from :class:`ArcSettings`; the restrictive tier multiplies them."""
        mult = 1.0
        if tier is not None and tier.restricted:
            mult = settings.order_budget_restrictive_dedupe_cooldown_multiplier

        def scaled(n: int) -> int:
            return int(round(n * mult)) if n else 0

        return cls(
            executed_sessions=scaled(settings.dedupe_executed_cooldown_sessions),
            proposed_sessions=scaled(settings.dedupe_proposed_cooldown_sessions),
            rejected_sessions=scaled(settings.dedupe_rejected_cooldown_sessions),
            reprice_move_pct=settings.dedupe_reprice_move_pct,
        )


class RecentIdea(BaseModel):
    """One prior occurrence of a fingerprint (or of a ``ticker|stance`` prefix)."""

    model_config = _FORBID

    fingerprint: str
    kind: Suppression
    at: _dt.datetime
    spot: Decimal | None = None
    regime: str | None = None
    ref: str = Field("", description="proposal hash / structure id")
    open_structure: bool = Field(False, description="An open structure still holds this idea")


class DedupeVerdict(BaseModel):
    """The outcome of :func:`check_idea`."""

    model_config = _FORBID

    suppressed: bool
    kind: Suppression | None = None
    prior: RecentIdea | None = None
    override: str | None = Field(
        default=None, description="spot_moved | regime_changed when re-admitted"
    )
    detail: str = ""

    @property
    def reason_code(self) -> ReasonCode | None:
        if self.override is not None:
            return ReasonCode.DEDUPE_OVERRIDE
        return self.kind.reason_code if self.kind is not None else None


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def strike_bucket(strike: Decimal | float, spot: Decimal | float) -> int:
    """``round(strike / (1% of spot))``: strikes within ~1% of spot share a bucket."""
    s, p = Decimal(str(strike)), Decimal(str(spot))
    if p <= 0:
        msg = "spot must be positive for a strike bucket"
        raise ValueError(msg)
    return int((s / (p * _BUCKET_PCT)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _iso_week(d: _dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _anchor_strike(st: Structure) -> Decimal:
    """The short strike (nearest to the money when several), else the long strike."""
    shorts = [parse_occ(leg.occ_symbol) for leg in st.legs if leg.side == LegIntent.SHORT]
    longs = [parse_occ(leg.occ_symbol) for leg in st.legs if leg.side == LegIntent.LONG]
    pool = shorts or longs
    if not pool:
        msg = "structure has no legs"
        raise ValueError(msg)
    if len(pool) == 1:
        return pool[0].strike
    # Iron condor / multi-short: the bucket is the mean of the short strikes, so the
    # same body at a different width still counts as the same idea.
    return sum((o.strike for o in pool), Decimal(0)) / len(pool)


def fingerprint(
    ticker: str,
    stance: Stance | str,
    st: Structure,
    spot: Decimal | float,
    *,
    structure_type: str | None = None,
) -> IdeaFingerprint:
    """Fingerprint a priced structure at *spot* (the underlying mid when proposed)."""
    kind = st.kind or StructureKind.OTHER
    stype = structure_type or STRUCTURE_TYPE_OF_KIND.get(kind, "other")
    expiry = min(parse_occ(leg.occ_symbol).expiration for leg in st.legs)
    return IdeaFingerprint(
        ticker=ticker.strip().upper(),
        stance=Stance(str(stance).strip().lower()),
        structure_type=stype,
        expiry_week=_iso_week(expiry),
        strike_bucket=strike_bucket(_anchor_strike(st), spot),
    )


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------


def sessions_back(now: _dt.datetime, sessions: int) -> _dt.datetime:
    """Start of the window: *sessions* trading sessions before *now*'s session.

    ``sessions == 0`` is the start of the current session (same-session only).
    """
    day = now.astimezone(ET).date()
    cur = day if is_session(day) else previous_session(day)
    for _ in range(sessions):
        cur = previous_session(cur)
    return _dt.datetime.combine(cur, _dt.time(0, 0), tzinfo=ET)


def _window(now: _dt.datetime, sessions: int) -> str:
    return sessions_back(now, sessions).astimezone(_dt.UTC).isoformat()


def _parse_ts(value: object) -> _dt.datetime:
    d = _dt.datetime.fromisoformat(str(value))
    return d if d.tzinfo else d.replace(tzinfo=_dt.UTC)


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


# ---------------------------------------------------------------------------
# Lookups (audit DB only)
# ---------------------------------------------------------------------------


def _proposal_rows(
    conn: sqlite3.Connection, *, since: str, ticker: str | None = None
) -> list[dict[str, Any]]:
    sql = (
        "SELECT p.proposal_hash, p.ticker, p.fingerprint, p.spot, p.regime, p.created_at, "
        "  p.structure_json, "
        "  r.status AS req_status, r.decided_by, "
        "  e.status AS exec_status, "
        "  o.status AS open_status, o.id AS structure_id, o.closed_at "
        "FROM proposals p "
        "LEFT JOIN approval_requests r ON r.proposal_hash = p.proposal_hash "
        "LEFT JOIN executions e ON e.proposal_hash = p.proposal_hash "
        "LEFT JOIN open_structures o ON o.open_proposal_hash = p.proposal_hash "
        "WHERE p.kind = 'open' AND p.fingerprint IS NOT NULL "
        "  AND (p.created_at >= ? OR o.status = 'open' OR o.closed_at >= ?)"
    )
    args: list[Any] = [since, since]
    if ticker is not None:
        sql += " AND p.ticker = ?"
        args.append(ticker)
    sql += " ORDER BY p.created_at"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _classify(row: dict[str, Any]) -> tuple[Suppression, bool] | None:
    """(kind, still_open) for one proposal row; ``None`` = nothing to hold against it."""
    if row.get("open_status") == "open":
        return Suppression.EXECUTED, True
    if row.get("open_status") == "closed":
        return Suppression.EXECUTED, False
    ex = row.get("exec_status")
    if ex in ("working", "filled", "partially_filled", "unconfirmed"):
        return Suppression.EXECUTED, False
    req = row.get("req_status")
    if req == "rejected":
        by = str(row.get("decided_by") or "")
        # only an owner click is a `rejected` idea; system TTL expiry is `proposed`
        return (
            (Suppression.REJECTED, False)
            if not by.startswith("arc:")
            else (
                Suppression.PROPOSED,
                False,
            )
        )
    if req == "approved" and ex in ("cancelled", "rejected"):
        return Suppression.PROPOSED, False  # approved but never filled: still an idea
    # pending / expired / not_actionable / no request yet / no execution
    return Suppression.PROPOSED, False


def recent_ideas(
    conn: sqlite3.Connection,
    *,
    now: _dt.datetime,
    cfg: DedupeConfig,
    ticker: str | None = None,
) -> list[RecentIdea]:
    """Every prior idea inside the widest cooldown window, latest first.

    Each row is classified (executed / proposed / rejected) and filtered by its
    own kind's cooldown, so the caller only sees ideas that still count.
    """
    widest = max(cfg.executed_sessions, cfg.proposed_sessions, cfg.rejected_sessions)
    rows = _proposal_rows(conn, since=_window(now, widest), ticker=ticker)
    out: list[RecentIdea] = []
    for row in rows:
        cls = _classify(row)
        if cls is None:
            continue
        kind, still_open = cls
        at = _parse_ts(row["created_at"])
        if kind is Suppression.EXECUTED and not still_open and row.get("closed_at"):
            at = max(at, _parse_ts(row["closed_at"]))  # cooldown counts from the close
        window = {
            Suppression.EXECUTED: cfg.executed_sessions,
            Suppression.PROPOSED: cfg.proposed_sessions,
            Suppression.REJECTED: cfg.rejected_sessions,
        }[kind]
        if not still_open and at < sessions_back(now, window):
            continue
        out.append(
            RecentIdea(
                fingerprint=str(row["fingerprint"]),
                kind=kind,
                at=at,
                spot=_dec(row.get("spot")),
                regime=row.get("regime"),
                ref=str(row.get("structure_id") or row["proposal_hash"]),
                open_structure=still_open,
            )
        )
    out.sort(key=lambda r: r.at, reverse=True)
    return out


_PRECEDENCE = (Suppression.EXECUTED, Suppression.REJECTED, Suppression.PROPOSED)


def check_idea(
    fp: IdeaFingerprint,
    priors: list[RecentIdea],
    *,
    spot: Decimal | float | None,
    regime: str | None,
    cfg: DedupeConfig,
) -> DedupeVerdict:
    """Is *fp* a repeat of one of *priors*? Applies the material-change override.

    An **open** structure is never overridden: the idea is held, full stop. For a
    closed / proposed / rejected prior the idea is re-admitted when spot moved at
    least ``reprice_move_pct`` since it, or the regime label changed.
    """
    matches = [p for p in priors if p.fingerprint == fp.key()]
    if not matches:
        return DedupeVerdict(suppressed=False)
    matches.sort(key=lambda p: (_PRECEDENCE.index(p.kind), -p.at.timestamp()))
    prior = matches[0]
    if prior.open_structure:
        return DedupeVerdict(
            suppressed=True,
            kind=prior.kind,
            prior=prior,
            detail=f"open structure {prior.ref} holds this idea",
        )
    override: str | None = None
    if spot is not None and prior.spot is not None and prior.spot > 0:
        move = abs(Decimal(str(spot)) - prior.spot) / prior.spot
        if cfg.reprice_move_pct > 0 and move >= Decimal(str(cfg.reprice_move_pct)):
            override = "spot_moved"
    if override is None and regime and prior.regime and regime != prior.regime:
        override = "regime_changed"
    if override is not None:
        return DedupeVerdict(
            suppressed=False,
            kind=prior.kind,
            prior=prior,
            override=override,
            detail=(
                f"re-admitted ({override}): spot {prior.spot} -> {spot}, "
                f"regime {prior.regime} -> {regime}"
            ),
        )
    return DedupeVerdict(
        suppressed=True,
        kind=prior.kind,
        prior=prior,
        detail=f"{prior.kind.value} {prior.at.astimezone(ET):%Y-%m-%d %H:%M} ({prior.ref[:12]})",
    )


def next_admissible(now: _dt.datetime, prior: RecentIdea, cfg: DedupeConfig) -> _dt.date | None:
    """The first session on which *prior* stops counting (``None`` while it is open)."""
    if prior.open_structure:
        return None
    n = {
        Suppression.EXECUTED: cfg.executed_sessions,
        Suppression.PROPOSED: cfg.proposed_sessions,
        Suppression.REJECTED: cfg.rejected_sessions,
    }[prior.kind]
    return add_sessions(prior.at.astimezone(ET).date(), n + 1)
