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
``missed``     approved, but the ladder ended ``cancelled`` with nothing filled for
               a no-fill reason (walked every step, stale band, market moved past
               the band) earlier in the **current** session (E24.2, D101)

Cooldowns are trading sessions (config) and are multiplied in the D32 restrictive
tier. A **material change** re-admits the idea: spot moved at least
``dedupe_reprice_move_pct`` since the last one, or Research's market regime
changed. Everything here is deterministic and reads only the audit DB.

E24.2 (D101) missed-open retry: a ``missed`` idea is held like ``proposed`` until
``dedupe_missed_retry_after_minutes`` have passed since its ladder finished, then
re-admitted, at most ``dedupe_missed_retry_max`` times per session (every later
proposal of the fingerprint counts as one retry). ``0`` retries is the rollback: a
miss is then plain ``proposed``, exactly as before E24.2. A miss from an earlier
session is ``proposed`` too, so no idea is retried across sessions by this rule.
The restrictive tier multiplies the wait and divides the retry cap (1 -> 0).

Two call sites (:mod:`arc.pipeline.steps`):

* Research stage, by ``(ticker, stance)`` prefix before the LLM call —
  suppressed names are listed to Research as "recently suggested / held" and
  names with an open structure in the same stance are removed outright;
* propose, by the full fingerprint, as the final check.
"""

from __future__ import annotations

import datetime as _dt
import enum
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Literal

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
    "MissedRetry",
    "Suppression",
    "check_idea",
    "fingerprint",
    "is_missed_detail",
    "recent_ideas",
    "sessions_back",
    "strike_bucket",
    "structure_label",
]

log = structlog.get_logger(__name__)

_FORBID = ConfigDict(extra="forbid")
_BUCKET_PCT = Decimal("0.01")

# Structure kinds to Research's structure-type vocabulary (steps.STRUCTURE_TYPES).
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
    MISSED = "missed"  # E24.2 (D101): approved, never filled, retry-eligible this session

    @property
    def reason_code(self) -> ReasonCode:
        return {
            Suppression.EXECUTED: ReasonCode.DEDUPE_EXECUTED,
            Suppression.PROPOSED: ReasonCode.DEDUPE_PROPOSED,
            Suppression.REJECTED: ReasonCode.DEDUPE_REJECTED,
            Suppression.MISSED: ReasonCode.DEDUPE_MISSED_WAIT,
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
        """Research-stage key: ``ticker|stance``."""
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
    # E24.2 (D101): the missed-open retry. 0 retries = a miss is plain `proposed`.
    missed_retry_after_minutes: int = Field(30, ge=0)
    missed_retry_max: int = Field(1, ge=0)

    @classmethod
    def from_settings(cls, settings: ArcSettings, tier: Tier | None = None) -> DedupeConfig:
        """Base cooldowns from :class:`ArcSettings`; the restrictive tier multiplies them.

        E24.2: the restrictive tier also multiplies the missed-retry wait and divides
        the retry cap (floor), so the default single retry is off in that tier.
        """
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
            missed_retry_after_minutes=scaled(settings.dedupe_missed_retry_after_minutes),
            missed_retry_max=int(settings.dedupe_missed_retry_max / mult),
        )


class MissedRetry(BaseModel):
    """E24.2 (D101): where a ``missed`` idea stands on its same-session retry at ``now``.

    ``wait`` = inside ``missed_retry_after`` (held like ``proposed``); ``retry`` =
    re-admitted; ``spent`` = the session's retries are used (held like ``proposed``).
    """

    model_config = _FORBID

    status: Literal["wait", "retry", "spent"]
    missed_at: _dt.datetime
    ready_at: _dt.datetime
    used: int = Field(..., ge=0, description="Later proposals of this idea this session")
    max: int = Field(..., ge=0)


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
    # E24.2 (D101): `missed` rows only (None / "" otherwise)
    retry: MissedRetry | None = None
    label: str = Field(default="", description="Plain structure label, e.g. 'put debit'")

    def dump(self) -> dict[str, Any]:
        """The recorded form; the E24.2 fields only when set (pre-E24.2 rows unchanged)."""
        unset = {k for k in ("retry", "label") if not getattr(self, k)}
        return self.model_dump(mode="json", exclude=unset)

    def retry_line(self) -> str:
        """``Retry: TSLA bearish put debit (missed 13:16 ET, 1/1)`` (missed rows only)."""
        assert self.retry is not None  # noqa: S101 - callers pass missed rows
        fp = IdeaFingerprint.parse(self.fingerprint)
        return (
            f"Retry: {fp.ticker} {fp.stance.value} {self.label or fp.structure_type} "
            f"(missed {self.retry.missed_at.astimezone(ET):%H:%M} ET, "
            f"{self.retry.used + 1}/{self.retry.max})"
        )


class DedupeVerdict(BaseModel):
    """The outcome of :func:`check_idea`."""

    model_config = _FORBID

    suppressed: bool
    kind: Suppression | None = None
    prior: RecentIdea | None = None
    override: str | None = Field(
        default=None, description="spot_moved | regime_changed when re-admitted"
    )
    retry: MissedRetry | None = Field(default=None, description="E24.2: the missed-open retry")
    detail: str = ""

    @property
    def reason_code(self) -> ReasonCode | None:
        if self.override is not None:
            return ReasonCode.DEDUPE_OVERRIDE
        if self.retry is not None:
            return {
                "retry": ReasonCode.DEDUPE_MISSED_RETRY,
                "wait": ReasonCode.DEDUPE_MISSED_WAIT,
                "spent": ReasonCode.DEDUPE_PROPOSED,  # retries used: held like `proposed`
            }[self.retry.status]
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
        "  e.status AS exec_status, e.filled_qty AS exec_filled, e.detail AS exec_detail, "
        "  e.finished_at AS exec_finished_at, "
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


#: E24.2 (D101): the ladder details of a no-fill cancel (:mod:`arc.execution.ladder`):
#: walked every step, stale band (``order:stale_band``), or the market moved past the
#: band (``order:band_moved``: no re-gate left, a failed re-gate, an empty re-gated
#: band). Budget stops, submit failures and halts are not misses.
_MISSED_DETAIL = re.compile(
    r"^(no fill after \d+ attempt\(s\)|stale band: |market moved past the band: "
    r"|re-gated band .* holds no valid price)"
)


def is_missed_detail(detail: object) -> bool:
    """Is *detail* (``executions.detail``) a no-fill reason? (E24.2, D101)."""
    return bool(_MISSED_DETAIL.match(str(detail or "")))


def _is_missed(row: dict[str, Any], session_start: _dt.datetime, cfg: DedupeConfig) -> bool:
    """E24.2: an approved open whose ladder ended ``cancelled``, nothing filled, for a
    no-fill reason, inside the current session. Off (always False) at 0 retries."""
    if cfg.missed_retry_max <= 0 or row.get("open_status") is not None:
        return False
    if row.get("req_status") != "approved" or row.get("exec_status") != "cancelled":
        return False
    if int(row.get("exec_filled") or 0) != 0 or not is_missed_detail(row.get("exec_detail")):
        return False
    finished = row.get("exec_finished_at")
    return bool(finished) and _parse_ts(finished) >= session_start


_STRUCTURE_LABEL: dict[StructureKind, str] = {
    StructureKind.LONG_CALL: "long call",
    StructureKind.LONG_PUT: "long put",
    StructureKind.IRON_CONDOR: "iron condor",
}


def structure_label(st: Structure) -> str:
    """Plain label: ``put debit`` / ``call credit`` / ``long call`` / ``iron condor``."""
    kind = st.kind or StructureKind.OTHER
    if kind in (StructureKind.VERTICAL_DEBIT, StructureKind.VERTICAL_CREDIT):
        rights = {parse_occ(leg.occ_symbol).kind.value for leg in st.legs}
        right = {"c": "call", "p": "put"}.get(next(iter(rights)), "") if len(rights) == 1 else ""
        side = "debit" if kind is StructureKind.VERTICAL_DEBIT else "credit"
        return f"{right} {side}".strip()
    return _STRUCTURE_LABEL.get(kind, STRUCTURE_TYPE_OF_KIND.get(kind, "other"))


def _label(structure_json: object) -> str:
    try:
        return structure_label(Structure.model_validate_json(str(structure_json)))
    except ValueError:
        return ""


def _missed_retry(
    miss: dict[str, Any], used: int, now: _dt.datetime, cfg: DedupeConfig
) -> MissedRetry:
    missed_at = _parse_ts(miss["exec_finished_at"])
    ready_at = missed_at + _dt.timedelta(minutes=cfg.missed_retry_after_minutes)
    status: Literal["wait", "retry", "spent"] = (
        "spent" if used >= cfg.missed_retry_max else "wait" if now < ready_at else "retry"
    )
    return MissedRetry(
        status=status, missed_at=missed_at, ready_at=ready_at, used=used, max=cfg.missed_retry_max
    )


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

    E24.2 (D101): per fingerprint, the session's latest no-fill miss is a ``missed``
    row carrying its :class:`MissedRetry`; it supersedes the fingerprint's earlier
    ``proposed`` rows (the idea went on to an approved ladder since). Every proposal
    of the fingerprint after the session's first miss counts as one retry used.
    """
    widest = max(cfg.executed_sessions, cfg.proposed_sessions, cfg.rejected_sessions)
    rows = _proposal_rows(conn, since=_window(now, widest), ticker=ticker)
    session_start = sessions_back(now, 0)
    latest_miss: dict[str, dict[str, Any]] = {}
    first_miss: dict[str, _dt.datetime] = {}
    for row in sorted(rows, key=lambda r: _parse_ts(r["created_at"])):
        if _is_missed(row, session_start, cfg):
            fp = str(row["fingerprint"])
            latest_miss[fp] = row
            first_miss.setdefault(fp, _parse_ts(row["created_at"]))
    out: list[RecentIdea] = []
    for row in rows:
        cls = _classify(row)
        if cls is None:
            continue
        kind, still_open = cls
        at = _parse_ts(row["created_at"])
        fp = str(row["fingerprint"])
        retry: MissedRetry | None = None
        miss = latest_miss.get(fp)
        if miss is row:
            since = first_miss[fp]
            used = sum(
                1 for r in rows if r["fingerprint"] == fp and _parse_ts(r["created_at"]) > since
            )
            kind, retry = Suppression.MISSED, _missed_retry(row, used, now, cfg)
        elif (
            miss is not None and kind is Suppression.PROPOSED and at < _parse_ts(miss["created_at"])
        ):
            continue  # superseded by the later miss of the same idea
        if kind is Suppression.EXECUTED and not still_open and row.get("closed_at"):
            at = max(at, _parse_ts(row["closed_at"]))  # cooldown counts from the close
        window = {
            Suppression.EXECUTED: cfg.executed_sessions,
            Suppression.PROPOSED: cfg.proposed_sessions,
            Suppression.REJECTED: cfg.rejected_sessions,
            Suppression.MISSED: cfg.proposed_sessions,
        }[kind]
        if not still_open and at < sessions_back(now, window):
            continue
        out.append(
            RecentIdea(
                fingerprint=fp,
                kind=kind,
                at=at,
                spot=_dec(row.get("spot")),
                regime=row.get("regime"),
                ref=str(row.get("structure_id") or row["proposal_hash"]),
                open_structure=still_open,
                retry=retry,
                label=_label(row.get("structure_json")) if retry is not None else "",
            )
        )
    out.sort(key=lambda r: r.at, reverse=True)
    return out


_PRECEDENCE = (
    Suppression.EXECUTED,
    Suppression.REJECTED,
    Suppression.PROPOSED,
    Suppression.MISSED,
)


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
    closed / proposed / rejected / missed prior the idea is re-admitted when spot
    moved at least ``reprice_move_pct`` since it, or the regime label changed.
    E24.2: a ``missed`` prior is otherwise re-admitted once its retry is due
    (``retry``) and held while it waits or once the session's retries are spent.
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
    if prior.retry is not None:
        r = prior.retry
        missed = f"missed {r.missed_at.astimezone(ET):%H:%M} ET ({prior.ref[:12]})"
        detail = {
            "retry": f"retry {r.used + 1}/{r.max}: {missed}",
            "wait": f"{missed}; retry from {r.ready_at.astimezone(ET):%H:%M} ET",
            "spent": f"{missed}; same-day retries used ({r.used}/{r.max})",
        }[r.status]
        return DedupeVerdict(
            suppressed=r.status != "retry", kind=prior.kind, prior=prior, retry=r, detail=detail
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
        Suppression.MISSED: cfg.proposed_sessions,
    }[prior.kind]
    return add_sessions(prior.at.astimezone(ET).date(), n + 1)
