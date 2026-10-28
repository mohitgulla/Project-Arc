"""``ml.score`` inside ``quant.open`` (E27.4, D126): shadow M2 scores of the Quant menu.

Runs after :func:`arc.pipeline.steps._quant_menus` has built every menu and **before**
the Quant call, so it scores exactly the items Quant is offered. It writes one
``ml_scores`` context entry (subject ``session``) and returns its id for the journal
links. Shadow only: no prompt reads the kind (the Quant prompt's inputs are built
from the menus, not the snapshot), and nothing returned here feeds a gate, sizing or
ranking decision.

Fail open: any error is logged, raises one ``ml:score_failed:<day>`` ops alert per
day (recorded resolved: informational), and the step carries on.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import structlog

from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from arc.context.store import ContextSnapshot
    from arc.ml.scores import MlScoresPayload
    from arc.routines.handlers import JobContext

log = structlog.get_logger(__name__)

#: ``ml_scores`` subject: one entry per ``quant.open`` run.
SUBJECT = "session"


@dataclasses.dataclass(frozen=True)
class ShadowResult:
    """What ``quant.open`` keeps from the shadow step (all ``None`` when off)."""

    entry_id: str | None = None
    payload: MlScoresPayload | None = None

    def link(self, key: str | None = None) -> dict[str, Any]:
        """Journal payload fields: the ``ml_scores`` entry id (+ the item's score)."""
        if self.entry_id is None:
            return {}
        out: dict[str, Any] = {"ml_scores_id": self.entry_id}
        if key is not None and self.payload is not None:
            s = self.payload.by_key().get(key)
            if s is not None and s.status == "ok":
                out["ml"] = {
                    "p_win": s.p_win,
                    "e_r": s.e_r,
                    "p_stop": s.p_stop,
                    "rank_m2": s.rank_m2,
                    "model_version": s.model_version,
                }
        return out


def link_from_snapshot(snapshot: ContextSnapshot, key: str) -> dict[str, Any]:
    """:meth:`ShadowResult.link` for a later step of the chain (``quant.propose``)."""
    from arc.ml.scores import MlScoresPayload

    entry = snapshot.latest("ml_scores", SUBJECT)
    if entry is None:
        return {}
    payload = entry.model()
    if not isinstance(payload, MlScoresPayload):  # pragma: no cover - registry guarantees
        return {}
    return ShadowResult(entry.id, payload).link(key)


def _bundle(ctx: JobContext) -> Any:
    from arc.ml.serve import family_for_profile, load_bundle_or_reason

    root = ctx.settings.ml_data_dir / "ml" / "models"
    return load_bundle_or_reason(root, family_for_profile(ctx.settings.account_profile))


def _m1(ctx: JobContext, bundle: Any) -> Any:
    if isinstance(bundle, str):
        return None
    from arc.ml.shadow import load_m1_text

    try:
        return load_m1_text(
            ctx.settings.ml_data_dir / "ml" / "models", bundle.manifest.get("m1_version")
        )
    except Exception as e:  # noqa: BLE001 - the M1 summary is optional context
        log.warning("ml.m1_load_failed", error=f"{type(e).__name__}: {e}")
        return None


def filter_reasons(
    conn: sqlite3.Connection, chain_run_id: str | None, credit_only: Collection[str]
) -> dict[str, str]:
    """The ledger's ``ctx_filter_reason`` per ``credit_only`` ticker: the D128 journal
    code this chain's Research step recorded (``stretched_debit_only`` /
    ``mid_range_debit_only``). Read only; empty without a chain or a credit-only name."""
    want = {t.upper() for t in credit_only}
    if not want or chain_run_id is None:
        return {}
    rows = conn.execute(
        """SELECT subject, reason_code FROM decisions
           WHERE chain_run_id = ? AND reason_code IN (?, ?) ORDER BY at, rowid""",
        (chain_run_id, "stretched_debit_only", "mid_range_debit_only"),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows if str(r[0]) in want}


def score_menus(
    ctx: JobContext,
    m: Any,
    *,
    credit_only: Collection[str] = (),
    filter_reasons: dict[str, str] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> ShadowResult:
    """Score every item of *m* (a ``_QuantMenus``) and write the ``ml_scores`` entry.

    Never raises: returns an empty :class:`ShadowResult` when ``ml_shadow`` is off or
    the step itself fails (the entry, when written, still carries ``unavailable`` rows
    with their reason for model / feature failures).
    """
    settings = ctx.settings
    if not settings.ml_shadow or not m.menus:
        return ShadowResult()
    t0 = clock()
    try:
        payload = _score(ctx, m, credit_only, filter_reasons or {}, clock)
        entry = ctx.write("ml_scores", SUBJECT, payload)
    except Exception as e:  # noqa: BLE001 - fail open: shadow scoring never fails the tick
        log.warning("ml.score_failed", error=f"{type(e).__name__}: {e}", exc_info=True)
        _alert(ctx, f"{type(e).__name__}: {e}")
        return ShadowResult()
    log.info(
        "ml.score",
        n=payload.n,
        n_ok=payload.n_ok,
        status=payload.status,
        model=payload.model_version,
        panel=payload.panel_source,
        latency_ms=round((clock() - t0) * 1000),
        reason=payload.reason,
    )
    if payload.status == "unavailable" and payload.n:
        _alert(ctx, payload.reason or "unavailable")
    return ShadowResult(entry.id, payload)


def _score(
    ctx: JobContext,
    m: Any,
    credit_only: Collection[str],
    filter_reasons: dict[str, str],
    clock: Callable[[], float],
) -> MlScoresPayload:
    from arc.control.effective import ranking_config
    from arc.ml.live_menu import from_scan
    from arc.ml.shadow import score_menu
    from arc.pipeline.analytics import regime_atr14
    from arc.pipeline.market import ETF_UNDERLYINGS, next_earnings

    settings = ctx.settings
    today = ctx.now.astimezone(ET).date()
    filters = ranking_config(settings).filters
    tickers = sorted(m.menus)
    earnings = next_earnings(ctx.conn, tickers, today)
    now_close = _dt.datetime.combine(today, _dt.time(16, 0), tzinfo=ET)
    cands = []
    for t in tickers:
        entry = ctx.snapshot.latest("regime", t)
        atr14 = regime_atr14(entry.payload if entry is not None else None)
        by_sleeve: dict[str | None, int] = {}
        for c in m.menus[t].values():
            sleeve = m.sleeve_of.get(id(c))
            by_sleeve[sleeve] = by_sleeve.get(sleeve, 0) + 1
            cands.append(
                from_scan(
                    c,
                    spot=float(m.spots[t]),
                    summary=m.summaries.get(id(c)),
                    settings=settings,
                    filters=filters,
                    now=now_close,
                    next_earnings=earnings,
                    sleeve=sleeve,
                    est_cost=m.est_cost.get(id(c)),
                    atr14=atr14,
                    credit_only=t in credit_only,
                    filter_reason=filter_reasons.get(t),
                    etf=t in ETF_UNDERLYINGS,
                    rank_live=by_sleeve[sleeve],
                )
            )
    bundle = _bundle(ctx)
    return score_menu(
        cands,
        day=today,
        bundle=bundle,
        conn=ctx.conn,
        data_dir=settings.ml_data_dir,
        etfs=frozenset(ETF_UNDERLYINGS),
        m1=_m1(ctx, bundle),
        clock=clock,
    )


def _alert(ctx: JobContext, reason: str) -> None:
    """One informational ``ml:score_failed:<day>`` ops alert per day (never fails)."""
    from arc.monitoring.store import AlertRepo

    day = ctx.now.astimezone(ET).date().isoformat()
    key = f"ml:score_failed:{day}"
    try:
        repo = AlertRepo(ctx.conn)
        if repo.seen(key):
            return
        repo.open(
            key,
            "ml:score_failed",
            f"ML shadow scoring unavailable ({reason[:200]}); trading is unaffected",
            at=ctx.now,
            correlation={"session": day, "reason": reason[:300]},
            resolved=True,
        )
    except sqlite3.Error as exc:
        log.warning("ml.alert_failed", error=str(exc))
