"""Nightly ``ml.refresh`` (E27.4, D126): live panels, M1 next day, drift, parity, health.

One run per trading day (20:45 ET, after ``history.topup``):

1. **live panel** of session *day* for the active list + open underlyings through the
   live adapter (:func:`arc.ml.features.adapters.live.live_inputs`, the same
   :func:`~arc.ml.features.compute.compute_panel` as training), written to
   ``<data>/ml/live/features/v<schema>/date=<day>.parquet``; the next session's
   ``ml.score`` reads it (:func:`arc.ml.live_rows.stored_panel`);
2. **M1 next day**: each ticker's 21-session read from that panel (``<data>/ml/live/m1/``);
3. **feature drift**: PSI of the live 20-session panels vs a sample of the serving
   model's training window (:func:`arc.ml.monitor.feature_drift`);
4. **parity**: live vs history panel on the newest overlapping session (the history
   cache lags a session: ``history.topup`` fetches through the previous session);
5. **score drift**: live ``ml_scores`` vs the serving model's backtest (out-of-fold)
   scores of the same calendar month;
6. the daily ``ml_health`` record (:func:`arc.ml.monitor.write_health`).

Every step fails open into the health record (``status: error`` + reason); the run only
fails when nothing could be written. Context only: no gate, sizing, ranking or prompt
reads any of this.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence

    import pandas as pd

log = structlog.get_logger(__name__)

#: live panels in the drift window (sessions)
DRIFT_SESSIONS = 20
#: training-window panels sampled for the drift reference (every n-th session)
TRAIN_SAMPLE_STRIDE = 10
#: names compared in the parity check (the history cache is read per name)
PARITY_MAX_NAMES = 25


@dataclass
class RefreshResult:
    day: dt.date
    panel_rows: int = 0
    panel_path: Path | None = None
    m1_rows: int = 0
    history_panels: dict[str, int] = field(default_factory=dict)
    health_path: Path | None = None
    health: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        cov = self.health.get("coverage", {})
        drift = self.health.get("feature_drift", {})
        par = self.health.get("parity", {})
        parts = [
            f"live panel {self.day}: {self.panel_rows} names",
            f"M1 {self.m1_rows}",
            f"scored {cov.get('scored', 0)}/{cov.get('candidates', 0)}",
            f"drift warn {len(drift.get('warn', []))}" if "warn" in drift else "drift –",
            f"parity {par.get('status', '–')}",
        ]
        if self.errors:
            parts.append(f"{len(self.errors)} step errors ({', '.join(sorted(self.errors))})")
        return " · ".join(parts)

    def metrics(self) -> dict[str, Any]:
        return {
            "panel_rows": self.panel_rows,
            "m1_rows": self.m1_rows,
            "history_panels": dict(self.history_panels),
            "errors": dict(self.errors),
            "seconds": dict(self.seconds),
        }


def m1_root(data_dir: Path) -> Path:
    return Path(data_dir) / "ml" / "live" / "m1"


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def write_live_panel(data_dir: Path, schema: str, day: dt.date, panel: pd.DataFrame) -> Path:
    """Store *panel* as session *day*'s live panel (one row per ticker)."""
    from arc.ml.features.store import date_path, version_dir
    from arc.ml.live_rows import live_panel_root

    p = date_path(version_dir(live_panel_root(data_dir), schema), day)
    _write_parquet(panel.reset_index(drop=True), p)
    return p


def live_panels(data_dir: Path, schema: str, day: dt.date, n: int) -> pd.DataFrame:
    """The newest *n* stored live panels dated ≤ *day* (concatenated; may be empty)."""
    import pandas as pd

    from arc.ml.features.store import version_dir
    from arc.ml.live_rows import live_panel_root

    vdir = version_dir(live_panel_root(data_dir), schema)
    if not vdir.is_dir():
        return pd.DataFrame()
    files = sorted(
        p for p in vdir.glob("date=*.parquet") if p.stem.split("=", 1)[1] <= day.isoformat()
    )[-n:]
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def train_sample(
    features_root: Path,
    schema: str,
    start: dt.date,
    end: dt.date,
    cols: Sequence[str],
    stride: int = TRAIN_SAMPLE_STRIDE,
) -> pd.DataFrame:
    """Every *stride*-th stored history panel in [start, end] (the drift reference)."""
    import pandas as pd

    from arc.ml.features.store import date_path, version_dir
    from arc.utils.calendar import sessions_between

    vdir = version_dir(features_root, schema)
    frames = []
    for d in sessions_between(start, end)[::stride]:
        p = date_path(vdir, d)
        if p.is_file():
            df = pd.read_parquet(p)
            frames.append(df[[c for c in ("date", "ticker", *cols) if c in df.columns]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


#: score-drift group keys for the All / Stocks / ETFs splits (kinds use their own name)
SPLIT_KEYS = {"stocks": "split:stocks", "etfs": "split:etfs"}


def score_drift_ref(
    models_root: Path,
    family: str,
    version: str,
    month: int,
    etfs: frozenset[str] = frozenset(),
) -> dict[str, dict[str, list[float]]]:
    """The serving model's out-of-fold (backtest) ``e_r``/``p_win`` for calendar *month*:
    ``all``, each kind, and ``split:stocks`` / ``split:etfs`` (by ticker)."""
    import pandas as pd

    p = Path(models_root) / family / version / "oos_scores.parquet"
    if not p.is_file():
        return {}
    import pyarrow.parquet as pq

    cols = ["date", "kind", "e_r", "p_win"]
    if "ticker" in pq.read_schema(p).names:
        cols.append("ticker")
    df = pd.read_parquet(p, columns=cols)
    df = df[pd.to_datetime(df["date"]).dt.month == month]

    def vals(g: pd.DataFrame) -> dict[str, list[float]]:
        return {k: g[k].dropna().astype(float).tolist() for k in ("e_r", "p_win")}

    out = {"all": vals(df)}
    for kind, g in df.groupby(df["kind"].astype(str)):
        out[str(kind)] = vals(g)
    if "ticker" in df.columns:
        is_etf = df["ticker"].astype(str).str.upper().isin(etfs)
        out[SPLIT_KEYS["stocks"]] = vals(df[~is_etf])
        out[SPLIT_KEYS["etfs"]] = vals(df[is_etf])
    return out


def live_score_values(
    entries: Sequence[dict[str, Any]], month: int, etfs: frozenset[str] = frozenset()
) -> dict[str, dict[str, list[float]]]:
    """Live ``ok`` scores of calendar *month* (``as_of``): ``all``, by kind, and
    ``split:stocks`` / ``split:etfs`` (by the score's ticker)."""
    from arc.ml.monitor import split_of

    def empty() -> dict[str, list[float]]:
        return {"e_r": [], "p_win": []}

    out: dict[str, dict[str, list[float]]] = {
        "all": empty(),
        SPLIT_KEYS["stocks"]: empty(),
        SPLIT_KEYS["etfs"]: empty(),
    }
    for e in entries:
        try:
            m = dt.date.fromisoformat(str(e.get("as_of"))[:10]).month
        except ValueError:
            continue
        if m != month:
            continue
        for s in e.get("scores", []):
            if s.get("status") != "ok":
                continue
            kind = str(s.get("kind"))
            split = SPLIT_KEYS[split_of(s.get("ticker"), etfs)]
            for k in ("e_r", "p_win"):
                if s.get(k) is not None:
                    v = float(s[k])
                    out["all"][k].append(v)
                    out[split][k].append(v)
                    out.setdefault(kind, empty())[k].append(v)
    return out


def score_drift_by_kind(
    live: dict[str, dict[str, list[float]]], ref: dict[str, dict[str, list[float]]]
) -> dict[str, Any]:
    """:func:`arc.ml.monitor.score_drift` for ``all``, the Stocks / ETFs splits
    (``by_split``) and every kind either side has (``by_kind``)."""
    from arc.ml.monitor import score_drift

    empty: dict[str, list[float]] = {"e_r": [], "p_win": []}
    splits = set(SPLIT_KEYS.values())
    kinds = sorted((set(live) | set(ref)) - {"all"} - splits)
    return {
        "all": score_drift(live.get("all", empty), ref.get("all", empty)),
        "by_split": {
            name: score_drift(live.get(key, empty), ref.get(key, empty))
            for name, key in SPLIT_KEYS.items()
        },
        "by_kind": {k: score_drift(live.get(k, empty), ref.get(k, empty)) for k in kinds},
    }


def parity_check(
    live: pd.DataFrame, hist: pd.DataFrame, features: Sequence[str], day: dt.date
) -> dict[str, Any]:
    """Live vs history panel on *day*: features over tolerance (``breach`` when any)."""
    from arc.ml.features.parity import compare_panels

    lv = live[live["date"] == day] if len(live) else live
    hv = hist[hist["date"] == day] if len(hist) else hist
    if lv.empty or hv.empty:
        return {"status": "not_run", "day": day.isoformat(), "detail": "no overlapping rows"}
    rep = compare_panels(lv, hv, features)
    bad = rep[~rep["ok"]]
    return {
        "status": "breach" if len(bad) else "ok",
        "day": day.isoformat(),
        "names": int(lv["ticker"].nunique()),
        "features": len(rep),
        "over_tolerance": int(len(bad)),
        "breached": bad["feature"].astype(str).tolist(),
        "detail": ", ".join(bad["feature"].astype(str).head(5)) if len(bad) else "",
    }


def parity_alert_message(parity: Mapping[str, Any]) -> str | None:
    """The ops-alert text for a parity breach (``None`` unless ``status`` is ``breach``)."""
    if parity.get("status") != "breach":
        return None
    feats = [str(f) for f in parity.get("breached") or []] or [
        f.strip() for f in str(parity.get("detail") or "").split(",") if f.strip()
    ]
    shown = ", ".join(feats[:10]) + (f" (+{len(feats) - 10} more)" if len(feats) > 10 else "")  # noqa: PLR2004
    return (
        f"ML feature parity breach on {parity.get('day')}: {parity.get('over_tolerance')} of "
        f"{parity.get('features')} features over tolerance (live vs history store, "
        f"{parity.get('names')} names): {shown or 'unnamed'}; shadow scoring only, trading "
        "is unaffected"
    )


def history_cache_end(
    data_dir: Path, day: dt.date, ref: str = "SPY", lookback_days: int = 30
) -> dt.date | None:
    """The newest session ≤ *day* the history OHLC cache holds for *ref*."""
    from arc.ml.features.adapters.history import load_ohlc

    px = load_ohlc(Path(data_dir), ref, day - dt.timedelta(days=lookback_days), day)
    if px is None or px.empty:
        return None
    return max(px.index)


def extend_history_store(
    hist_dir: Path, *, universe: str, end: dt.date, back_days: int = 14
) -> dict[str, int]:
    """Write the history feature panels (E27.1 store) of the sessions in the last
    *back_days* through *end* that are still missing (``build_store`` skips the rest).
    *end* must be a session the history cache already holds, never a later one: a panel
    written before its data arrived would be frozen incomplete."""
    from arc.ml.features.adapters.history import load_membership, universe_symbols
    from arc.ml.features.store import build_store

    syms, etfs = universe_symbols(hist_dir, universe)
    return build_store(
        hist_dir,
        hist_dir / "ml" / "features",
        syms,
        end - dt.timedelta(days=back_days),
        end,
        etfs=etfs,
        members=load_membership(hist_dir),
        universe=universe,
    )


def _step(res: RefreshResult, name: str, fn: Callable[[], Any], clock: Callable[[], float]) -> Any:
    t0 = clock()
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - fail open into the health record
        res.errors[name] = f"{type(e).__name__}: {e}"[:300]
        log.warning("ml.refresh.step_failed", step=name, error=res.errors[name], exc_info=True)
        return None
    finally:
        res.seconds[name] = round(clock() - t0, 2)


def run_refresh(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    day: dt.date,
    tickers: Sequence[str],
    family: str,
    etfs: frozenset[str] = frozenset(),
    history_data_dir: Path | None = None,
    universe: str = "c500",
    extend_store: bool = True,
    clock: Callable[[], float] = time.perf_counter,
    live_inputs_fn: Callable[..., Any] | None = None,
    history_inputs_fn: Callable[..., Any] | None = None,
) -> RefreshResult:
    """One nightly refresh for session *day* (see the module doc). Never raises on a
    step failure; the health record always gets written."""
    import pandas as pd

    from arc.ml import monitor as mon
    from arc.ml.features.compute import compute_panel
    from arc.ml.features.registry import panel_feature_names, registry, schema_version
    from arc.ml.live_rows import latest_panel
    from arc.ml.serve import load_bundle_or_reason
    from arc.ml.shadow import bundle_panel_cols, load_m1_text, m1_summaries
    from arc.utils.calendar import previous_session

    if live_inputs_fn is None:
        from arc.ml.features.adapters.live import live_inputs as live_inputs_fn
    if history_inputs_fn is None:
        from arc.ml.features.adapters.history import history_inputs as history_inputs_fn

    data_dir = Path(data_dir)
    hist_dir = Path(history_data_dir or data_dir)
    models_root = data_dir / "ml" / "models"
    schema = schema_version()
    res = RefreshResult(day=day)
    names = sorted({t.upper() for t in tickers})
    bundle = load_bundle_or_reason(models_root, family)
    bundle_err = bundle if isinstance(bundle, str) else None

    # 1. live panel of `day` (window back to the previous session for the parity day)
    prev = previous_session(day)
    inp = _step(
        res,
        "inputs",
        lambda: live_inputs_fn(conn, names, prev, day, data_dir=hist_dir, etfs=etfs),
        clock,
    )
    panel = pd.DataFrame()
    window = pd.DataFrame()
    if inp is not None:
        window = (
            _step(res, "panel", lambda: compute_panel(inp, [prev, day], names), clock)
            if names
            else pd.DataFrame()
        )
        if window is None:
            window = pd.DataFrame()
        panel = _step(res, "latest", lambda: latest_panel(inp, day, names), clock)
        if panel is not None and len(panel):
            res.panel_path = write_live_panel(data_dir, schema, day, panel)
            res.panel_rows = len(panel)
        elif panel is None:
            panel = pd.DataFrame()

    # 2. M1 next-day read
    def _m1() -> int:
        if bundle_err is not None or panel.empty:
            return 0
        m1 = load_m1_text(models_root, bundle.manifest.get("m1_version"))  # type: ignore[union-attr]
        if m1 is None:
            return 0
        rows = pd.DataFrame([s.model_dump(mode="json") for s in m1_summaries(m1, panel)])
        if rows.empty:
            return 0
        _write_parquet(rows, m1_root(data_dir) / f"date={day.isoformat()}.parquet")
        return len(rows)

    res.m1_rows = _step(res, "m1", _m1, clock) or 0

    # 3. feature drift: live 20 sessions vs the serving model's training window
    def _drift() -> dict[str, Any]:
        if bundle_err is not None:
            return {"status": "not_run", "detail": bundle_err}
        cols = bundle_panel_cols(bundle)  # type: ignore[arg-type]
        tw = bundle.manifest.get("train_window") or {}  # type: ignore[union-attr]
        start = dt.date.fromisoformat(str(tw.get("start", "2020-01-01")))
        end = dt.date.fromisoformat(str(tw.get("end", "2023-12-31")))
        train = train_sample(hist_dir / "ml" / "features", schema, start, end, cols)
        live = live_panels(data_dir, schema, day, DRIFT_SESSIONS)
        rec = mon.feature_drift(train, live, cols)
        rec["live_sessions"] = int(live["date"].nunique()) if len(live) else 0
        rec["train_window"] = {"start": start.isoformat(), "end": end.isoformat()}
        return rec

    drift = _step(res, "drift", _drift, clock)

    # 4. parity on the newest session both stores hold: the history cache lags (topup
    #    fetches through the previous session), so first extend the E27.1 history store
    #    to the cache's last session, then compare its stored panel with the live one
    def _parity() -> dict[str, Any]:
        from arc.ml.features.store import date_path, version_dir

        cmp = sorted(names)[:PARITY_MAX_NAMES]
        if not cmp or window.empty:
            return {"status": "not_run", "detail": "no live panel"}
        end = history_cache_end(hist_dir, day)
        if end is None:
            return {"status": "not_run", "detail": "no history cache"}
        if extend_store:
            res.history_panels = extend_history_store(hist_dir, universe=universe, end=end)
        p = date_path(version_dir(hist_dir / "ml" / "features", schema), end)
        if p.is_file():
            hist = pd.read_parquet(p)
            hist = hist[hist["ticker"].isin(cmp)]
        else:
            hi = history_inputs_fn(hist_dir, cmp, end, end, etfs=etfs)
            hist = compute_panel(hi, [end], cmp)
        live = window
        if end not in set(window["date"]):
            li = live_inputs_fn(conn, cmp, end, end, data_dir=hist_dir, etfs=etfs)
            live = compute_panel(li, [end], cmp)
        reg = registry()
        feats = [f for f in panel_feature_names() if reg[f].available_live]
        return parity_check(live[live["ticker"].isin(cmp)], hist, feats, end)

    parity = _step(res, "parity", _parity, clock)

    # 5. score drift: live ml_scores vs the model's backtest scores, same month, by kind
    def _score_drift() -> dict[str, Any]:
        if bundle_err is not None:
            return {}
        since = day - dt.timedelta(days=31)
        entries = mon.score_entries(conn, since)
        live = live_score_values(entries, day.month, etfs)
        ref = score_drift_ref(models_root, bundle.family, bundle.version, day.month, etfs)  # type: ignore[union-attr]
        return score_drift_by_kind(live, ref)

    sdrift = _step(res, "score_drift", _score_drift, clock)

    # 6. the ml_health record
    def _health() -> Path:
        from arc.ml.registry import rolling_reads

        extra: dict[str, Any] = {
            "score_drift": sdrift or {},
            "serving": (
                {"family": family, "status": "unavailable", "reason": bundle_err}
                if bundle_err
                else {
                    "family": bundle.family,  # type: ignore[union-attr]
                    "version": bundle.version,  # type: ignore[union-attr]
                    "feature_schema": bundle.feature_schema,  # type: ignore[union-attr]
                }
            ),
            "refresh": {
                "panel_rows": res.panel_rows,
                "m1_rows": res.m1_rows,
                "names": len(names),
                "errors": dict(res.errors),
                "seconds": dict(res.seconds),
            },
        }
        reads = rolling_reads(models_root, family) if models_root.is_dir() else []
        if reads:
            extra["last_retrain"] = reads[-1]
        rec = mon.health(
            conn,
            day=day,
            since=day - dt.timedelta(days=DRIFT_SESSIONS),
            drift=drift
            if drift is not None
            else {"status": "error", "detail": res.errors.get("drift")},
            parity=(
                parity
                if parity is not None
                else {"status": "error", "detail": res.errors.get("parity")}
            ),
            extra=extra,
            etfs=etfs,
        )
        res.health = rec
        return mon.write_health(data_dir, rec)

    res.health_path = _step(res, "health", _health, clock)
    log.info(
        "ml.refresh",
        day=day.isoformat(),
        panel_rows=res.panel_rows,
        m1_rows=res.m1_rows,
        errors=sorted(res.errors),
        health=str(res.health_path) if res.health_path else None,
    )
    return res


def latest_health(data_dir: Path, day: dt.date) -> dict[str, Any] | None:
    """The newest ``ml_health`` record dated ≤ *day* (``arc ml monitor`` reuses its
    drift / parity / score-drift sections)."""
    hdir = Path(data_dir) / "ml" / "health"
    if not hdir.is_dir():
        return None
    files = sorted(p for p in hdir.glob("*.json") if p.stem <= day.isoformat())
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (OSError, ValueError):
        return None


def refresh_tickers(conn: sqlite3.Connection, settings: Any, now: dt.datetime) -> list[str]:
    """The names the nightly refresh covers: the day's active list ∪ open underlyings ∪
    the market reference (SPY/QQQ/IWM), i.e. every name the live loop may score."""
    from arc.universe.tiers import active_tickers, market_reference, open_underlyings

    names = set(active_tickers(conn, settings, now)) | set(market_reference(settings))
    try:
        names |= set(open_underlyings(conn))
    except Exception:  # noqa: BLE001 - a store without the books table still refreshes
        log.warning("ml.refresh.open_underlyings_failed", exc_info=True)
    return sorted(names)
