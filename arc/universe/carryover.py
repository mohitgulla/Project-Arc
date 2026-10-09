"""D64 (card E14.7): 48 h two-run carry-over for the discovery and trending tiers.

Each run of the Scout (``discovery``) or ``universe.trending`` (``trending``) writes a
``universe_tier`` entry **merged** with the previous run's entry, so up to two
consecutive runs inform a name's score and rank. Deterministic code, no LLM.

Rules (:func:`merge_members`, pure):

* prev = the latest ``universe_tier`` entry for the tier whose ``valid_from`` is within
  ``window_h`` (48) hours of this run and on an **earlier ET day** (a manual re-run never
  merges today with today). Any status: a same-day re-run finds yesterday's entry
  superseded by today's first run, and still merges it.
* Only prev's **own** names count (its ``score_today``, or the score parsed from its
  ``reason`` for pre-D64 rows); a name prev itself carried is not carried again, so a
  name never informs more than two runs.
* ``score = round(w_today × (today or 0) + w_prev × (prev or 0), 4)`` with
  ``w_prev = 1 − w_today``. Order: score desc, today's rank, prev rank, ticker. Cut to
  the tier size, ranks 1..n.
* Prev-only (carried) names keep prev's source and reason details plus
  ``· carried from <date>``; they are re-checked against today's exclusions (the
  writer passes them), never re-screened.
* ``reason`` keeps its trailing ``· score x.xx``, now the combined score.
* Switched off (``universe.carryover.enabled: false``): the list is this run's only,
  in this run's order (``score`` = this run's own score).
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import sqlite3
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.universe.tiers import CarryoverSettings, TierMember, UniverseTierPayload

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from arc.universe.tiers import Tier

log = structlog.get_logger(__name__)

__all__ = [
    "CarryoverSettings",
    "MergeResult",
    "apply_carryover",
    "journal_carried",
    "merge_members",
    "own_score",
    "parse_reason_score",
    "previous_entry",
]

_SCORE_RE = re.compile(r"\s*·?\s*score\s+(-?[0-9]*\.?[0-9]+)\s*$")
_CARRIED_RE = re.compile(r"\s*·\s*carried from \d{4}-\d{2}-\d{2}")


def parse_reason_score(reason: str) -> float | None:
    """The trailing ``score x.xx`` of a member reason (pre-D64 rows), else ``None``."""
    m = _SCORE_RE.search(reason or "")
    if m is None:
        return None
    try:
        v = float(m.group(1))
    except ValueError:  # pragma: no cover - the regex only matches numbers
        return None
    return v if math.isfinite(v) else None


def _strip_score(reason: str) -> str:
    """*reason* without its trailing score and any ``· carried from`` tag."""
    base = _SCORE_RE.sub("", reason or "")
    return _CARRIED_RE.sub("", base).strip()


def _with_score(base: str, score: float) -> str:
    return f"{base} · score {score:.2f}" if base else f"score {score:.2f}"


def own_score(m: TierMember) -> float | None:
    """*m*'s score in the run that wrote its entry (``None`` = carried into that entry).

    D64 members carry ``score_today`` (``None`` on a carried name); a pre-D64 member
    (no ``runs``) is its run's own name, scored from its ``reason``.
    """
    if m.score_today is not None:
        return m.score_today
    if m.runs:  # a D64 member without a score of its own was carried
        return None
    return parse_reason_score(m.reason)


class MergeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    members: list[TierMember]
    carried: list[TierMember] = Field(default_factory=list, description="prev-only names kept")
    excluded: dict[str, str] = Field(
        default_factory=dict, description="prev-only names dropped by today's exclusions"
    )
    cut: list[str] = Field(default_factory=list, description="names past the tier size")


def merge_members(
    today: Sequence[TierMember],
    prev: Sequence[TierMember] | None,
    *,
    today_day: _dt.date,
    prev_day: _dt.date | None,
    cfg: CarryoverSettings,
    size: int,
    excluded: Callable[[str], str | None] | None = None,
) -> MergeResult:
    """Merge this run's *today* members (``score_today`` set, ranked) with *prev*'s own
    names. *prev* ``None`` or *cfg* off = this run only. Pure and deterministic."""
    t_rank = {m.ticker: m.rank for m in today}
    t_by = {m.ticker: m for m in today}
    p_by: dict[str, TierMember] = {}
    p_score: dict[str, float] = {}
    if cfg.enabled and prev is not None and prev_day is not None:
        for m in sorted(prev, key=lambda r: r.rank):
            s = own_score(m)
            if s is None or m.ticker in p_by:
                continue
            p_by[m.ticker] = m
            p_score[m.ticker] = s
    p_rank = {t: i for i, t in enumerate(p_by, 1)}
    if not cfg.enabled:
        w_t, w_p = 1.0, 0.0
    else:
        w_t, w_p = cfg.w_today, cfg.w_prev
    out_excluded: dict[str, str] = {}
    rows: list[tuple[float, float, float, str, TierMember]] = []
    big = float("inf")
    for sym in dict.fromkeys([*t_by, *p_by]):
        tm, pm = t_by.get(sym), p_by.get(sym)
        st = tm.score_today if tm is not None else None
        sp = p_score.get(sym)
        if tm is None:  # carried: re-check today's exclusions (never re-screened)
            why = excluded(sym) if excluded is not None else None
            if why is not None:
                out_excluded[sym] = why
                continue
        score = round(w_t * (st or 0.0) + w_p * (sp or 0.0), 4)
        runs = sorted({d for d, s in ((today_day, st), (prev_day, sp)) if s is not None and d})
        if tm is not None:
            base = _strip_score(tm.reason)
            upd: dict[str, Any] = {}
        else:
            assert pm is not None and prev_day is not None
            base = f"{_strip_score(pm.reason)} · carried from {prev_day.isoformat()}".lstrip(" ·")
            upd = {"inputs": pm.inputs, "as_of": today_day}
        src = tm if tm is not None else pm
        assert src is not None
        member = src.model_copy(
            update={
                **upd,
                "reason": _with_score(base, score),
                "score": score,
                "score_today": st,
                "score_prev": sp,
                "runs": runs,
                "also_in": [],
            }
        )
        rows.append((-score, t_rank.get(sym, big), p_rank.get(sym, big), sym, member))
    if cfg.enabled:
        rows.sort(key=lambda r: r[:4])
    else:  # off: this run's own order, unchanged
        rows.sort(key=lambda r: (r[1], r[3]))
    kept = rows[: max(size, 0)]
    members = [r[4].model_copy(update={"rank": i}) for i, r in enumerate(kept, 1)]
    return MergeResult(
        members=members,
        carried=[m for m in members if m.score_today is None],
        excluded=out_excluded,
        cut=[r[3] for r in rows[max(size, 0) :]],
    )


def previous_entry(
    conn: sqlite3.Connection, tier: Tier, *, now: _dt.datetime, window_h: int
) -> tuple[str, _dt.datetime, UniverseTierPayload] | None:
    """``(entry id, valid_from, payload)`` of the latest *tier* entry written within
    *window_h* hours before *now* and on an earlier ET day (any status), else ``None``."""
    from arc.context.ttl import from_db, to_db
    from arc.utils.calendar import ET

    local = now.astimezone(ET)
    day_start = _dt.datetime(local.year, local.month, local.day, tzinfo=ET)
    lo = local - _dt.timedelta(hours=window_h)
    try:
        row = conn.execute(
            "SELECT id, payload, valid_from FROM context_entries WHERE kind = 'universe_tier' "
            "AND subject = ? AND valid_from >= ? AND valid_from < ? "
            "ORDER BY valid_from DESC, rowid DESC LIMIT 1",
            (tier.value, to_db(lo), to_db(min(day_start, local))),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return str(row[0]), from_db(row[2]), UniverseTierPayload.model_validate_json(row[1])


def apply_carryover(
    conn: sqlite3.Connection,
    payload: UniverseTierPayload,
    *,
    now: _dt.datetime,
    cfg: CarryoverSettings,
    size: int,
    excluded: Callable[[str], str | None] | None = None,
) -> tuple[UniverseTierPayload, MergeResult]:
    """This run's *payload* merged with the previous run's entry (see module doc).

    *payload* members must carry ``score_today`` (their own run score). Off = the
    members get their D64 fields and keep this run's order; ``merge`` is ``None``.
    """
    from arc.utils.calendar import ET

    today_day = now.astimezone(ET).date()
    prev = (
        previous_entry(conn, payload.tier, now=now, window_h=cfg.window_h) if cfg.enabled else None
    )
    res = merge_members(
        payload.members,
        prev[2].members if prev else None,
        today_day=today_day,
        prev_day=prev[1].astimezone(ET).date() if prev else None,
        cfg=cfg,
        size=size,
        excluded=excluded,
    )
    out = payload.model_copy(
        update={
            "members": res.members,
            "merged_from": prev[0] if prev else None,
            "merge": cfg.knobs() if cfg.enabled else None,
        }
    )
    log.info(
        "universe.carryover",
        tier=payload.tier.value,
        enabled=cfg.enabled,
        merged_from=prev[0] if prev else None,
        today=len(payload.members),
        carried=[m.ticker for m in res.carried],
        excluded=res.excluded,
        cut=res.cut,
    )
    return out, res


def journal_carried(
    conn: sqlite3.Connection,
    res: MergeResult,
    *,
    tier: Tier,
    at: _dt.datetime,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> int:
    """One ``universe:carried_over`` decision per carried name, idempotent per ET day
    and ticker (a re-run journals nothing new). Returns the rows written."""
    from arc.context.ttl import to_db
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore
    from arc.utils.calendar import ET

    if not res.carried:
        return 0
    local = at.astimezone(ET)
    start = _dt.datetime(local.year, local.month, local.day, tzinfo=ET)
    done = {
        r[0]
        for r in conn.execute(
            "SELECT subject FROM decisions WHERE reason_code = ? AND at >= ? AND at < ?",
            (
                ReasonCode.UNIVERSE_CARRIED_OVER.value,
                to_db(start),
                to_db(start + _dt.timedelta(days=1)),
            ),
        ).fetchall()
    }
    store = JournalStore(conn)
    n = 0
    with conn:
        for m in res.carried:
            if m.ticker in done:
                continue
            frm = m.runs[0].isoformat() if m.runs else None
            store.record(
                persona=JournalPersona.SYSTEM,
                stage=Stage.CANDIDATE,
                subject=m.ticker,
                choice=Choice.SELECTED,
                reason_code=ReasonCode.UNIVERSE_CARRIED_OVER,
                reason_text=f"{tier.value} #{m.rank} carried from {frm}: {m.reason}"[:500],
                confidence=min(max(m.score or 0.0, 0.0), 1.0),
                at=at,
                run_id=run_id,
                chain_run_id=chain_run_id,
                payload={
                    "tier": tier.value,
                    "score": m.score,
                    "score_prev": m.score_prev,
                    "from": frm,
                },
            )
            n += 1
    return n
