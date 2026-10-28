"""Live shadow scoring of the Quant menu (E27.4, D126): ``ml.score`` inside ``quant.open``.

:func:`score_menu` turns the live menu items (:class:`~arc.ml.live_rows.LiveCandidate`)
into an :class:`MlScoresPayload` — the ``ml_scores`` context entry: one
:class:`~arc.ml.serve.MlScore` per candidate (``p_win``, ``e_r``, ``p_stop``,
``r_q10/50/90``, ``e_days``, model version, feature digest, status), the rank the live
ranker gave it and the rank M2 would give, plus each ticker's M1 summary when an M1
version is registered.

**Shadow only.** Nothing here reaches ``arc/gate/``, the ``Candidate`` gate contract,
sizing, ranking or a persona prompt (import contracts + byte-identity tests). Every
failure is fail open: rows come back ``unavailable`` with a reason, and the caller
logs and moves on.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

from arc.ml.scores import (
    M1Summary,
    MlScore,
    MlScoresPayload,
    PanelSource,
    ScoredCandidate,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    import pandas as pd

    from arc.ml.live_rows import LiveCandidate
    from arc.ml.serve import Bundle

log = structlog.get_logger(__name__)


def bundle_panel_cols(bundle: Bundle) -> list[str]:
    """The bundle's numeric features that come from the panel (not the candidate row:
    neither a derived candidate feature nor a raw ledger column, which training reads
    from the ledger row itself)."""
    from arc.ml.m2.dataset import CAND_NUMERIC, LEDGER_FEATURE_COLS

    cand = set(CAND_NUMERIC) | set(LEDGER_FEATURE_COLS)
    return [f for f in bundle.features if not f.startswith("cat_") and f not in cand]


def _panel(
    conn: sqlite3.Connection | None,
    data_dir: Path,
    schema: str,
    day: dt.date,
    tickers: Sequence[str],
    *,
    etfs: frozenset[str],
    compute_missing: bool,
) -> tuple[pd.DataFrame, PanelSource]:
    """Panel rows per ticker: ``ml.refresh``'s stored panel first, then (fail open)
    the live adapter for the names it lacks. Features are the prior close's."""
    import pandas as pd

    from arc.ml.live_rows import latest_panel, stored_panel

    prev = day - dt.timedelta(days=1)
    stored = stored_panel(data_dir, schema, prev, tickers)
    have = set(stored["ticker"].astype(str)) if len(stored) else set()
    missing = [t for t in tickers if t not in have]
    if not missing or not compute_missing or conn is None:
        return stored, ("stored" if len(stored) else "none")
    from arc.ml.features.adapters.live import live_inputs

    inp = live_inputs(conn, missing, prev, prev, data_dir=data_dir, etfs=etfs)
    comp = latest_panel(inp, prev, missing)
    if comp.empty:
        return stored, ("stored" if len(stored) else "none")
    out = pd.concat([stored, comp], ignore_index=True) if len(stored) else comp
    return out, "computed"


def m1_summaries(m1: Any, panel: pd.DataFrame) -> list[M1Summary]:
    """Each panel ticker's M1 21-session read (empty without a loaded M1)."""
    if m1 is None or panel.empty:
        return []
    from arc.ml.m1.predict import publish

    pub = publish(m1, panel)
    out: list[M1Summary] = []

    def f(r: dict[str, Any], k: str) -> float | None:
        v = r.get(k)
        try:
            x = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return round(x, 6) if math.isfinite(x) else None

    for r in pub.to_dict(orient="records"):
        out.append(
            M1Summary(
                ticker=str(r["ticker"]),
                version=m1.manifest.version,
                p_up_h21=f(r, "m1_p_up_h21"),
                q10_h21=f(r, "m1_q10_h21"),
                q50_h21=f(r, "m1_q50_h21"),
                q90_h21=f(r, "m1_q90_h21"),
                rv_h21=f(r, "m1_rv_h21"),
                move_vs_implied_h21=f(r, "m1_move_vs_implied_h21"),
                direction=f(r, "m1_direction"),
            )
        )
    return out


def load_m1_text(models_root: Path, version: str | None) -> Any:
    """The M1 a bundle names (manifest ``m1_version``), loaded with text boosters (no
    lightgbm); ``None`` when unnamed or missing. Raises on a corrupt / stale artefact."""
    if not version:
        return None
    vdir = Path(models_root) / "m1" / version
    if not (vdir / "manifest.json").is_file():
        return None
    from arc.ml.m1.train import load_model

    return load_model(vdir, text=True)


def m2_ranks(scores: Sequence[MlScore]) -> dict[str, int]:
    """Rank (1 = best by ``e_r``, key tie-break) M2 would give each scored item within
    its (ticker, sleeve) menu: the unit the live ranker cuts (D97 sleeves)."""
    groups: dict[tuple[str, str | None], list[MlScore]] = {}
    for s in scores:
        if s.status == "ok" and s.e_r is not None:
            groups.setdefault((s.ticker, s.sleeve), []).append(s)
    out: dict[str, int] = {}
    for items in groups.values():
        for n, s in enumerate(sorted(items, key=lambda s: (-(s.e_r or 0.0), s.key)), 1):
            out[s.key] = n
    return out


def _unavailable(cands: Sequence[LiveCandidate], reason: str) -> list[MlScore]:
    return [
        MlScore(
            key=c.key,
            ticker=c.ticker,
            kind=c.kind,
            sleeve=c.sleeve,
            status="unavailable",
            reason=reason[:300],
        )
        for c in cands
    ]


def score_menu(
    cands: Sequence[LiveCandidate],
    *,
    day: dt.date,
    bundle: Bundle | str,
    conn: sqlite3.Connection | None,
    data_dir: Path,
    etfs: frozenset[str] = frozenset(),
    compute_missing: bool = True,
    m1: Any = None,
    clock: Callable[[], float] = time.perf_counter,
) -> MlScoresPayload:
    """Score every live menu candidate (fail open: never raises on model/data errors).

    *bundle* is a loaded M2 bundle or the refusal reason from
    :func:`~arc.ml.serve.load_bundle_or_reason`; *m1* an optional loaded M1 (its
    per-ticker summary is context only). Panel features are the prior session's close.
    """
    import pandas as pd

    from arc.ml.live_rows import menu_rows
    from arc.ml.serve import score

    t0 = clock()
    base: dict[str, Any] = {"as_of": day}
    if isinstance(bundle, str):
        return _payload(
            base, cands, _unavailable(cands, bundle), {}, None, "none", [], bundle, t0, clock
        )
    base |= {
        "family": bundle.family,
        "model_version": bundle.version,
        "feature_schema": bundle.feature_schema,
    }
    tickers = sorted({c.ticker for c in cands})
    try:
        panel, source = _panel(
            conn,
            data_dir,
            bundle.feature_schema,
            day,
            tickers,
            etfs=etfs,
            compute_missing=compute_missing,
        )
        scores = score(menu_rows(cands, panel, bundle_panel_cols(bundle)), bundle)
    except Exception as e:  # noqa: BLE001 - fail open
        reason = f"{type(e).__name__}: {e}"
        return _payload(
            base, cands, _unavailable(cands, reason), {}, None, "none", [], reason, t0, clock
        )
    pdate = max(pd.to_datetime(panel["date"]).dt.date) if len(panel) and "date" in panel else None
    summaries: list[M1Summary] = []
    if m1 is not None:
        base["m1_version"] = m1.manifest.version
        try:
            summaries = m1_summaries(m1, panel)
        except Exception as e:  # noqa: BLE001 - M1 context is optional
            log.warning("ml.m1_summary_failed", error=f"{type(e).__name__}: {e}")
    return _payload(
        base, cands, scores, m2_ranks(scores), pdate, source, summaries, None, t0, clock
    )


def _payload(
    base: dict[str, Any],
    cands: Sequence[LiveCandidate],
    scores: Sequence[MlScore],
    m2: dict[str, int],
    panel_date: dt.date | None,
    source: PanelSource,
    m1: list[M1Summary],
    reason: str | None,
    t0: float,
    clock: Callable[[], float],
) -> MlScoresPayload:
    by_key = {c.key: c for c in cands}
    out: list[ScoredCandidate] = []
    for s in scores:
        c = by_key.get(s.key)
        out.append(
            ScoredCandidate(
                **s.model_dump(),
                rank_live=None if c is None else c.rank_live,
                rank_m2=m2.get(s.key),
                managed_net_ev=None if c is None else c.managed_net_ev,
            )
        )
    n_ok = sum(1 for s in out if s.status == "ok")
    status: Literal["ok", "partial", "unavailable"] = (
        "ok" if out and n_ok == len(out) else "partial" if n_ok else "unavailable"
    )
    if reason is None and status == "unavailable":
        reasons = sorted({s.reason for s in out if s.reason})
        reason = "; ".join(reasons) if reasons else "no menu candidates"
    return MlScoresPayload(
        **base,
        panel_date=panel_date,
        panel_source=source,
        status=status,
        reason=None if reason is None else reason[:300],
        n=len(out),
        n_ok=n_ok,
        latency_ms=round((clock() - t0) * 1000),
        scores=out,
        m1=m1,
    )
