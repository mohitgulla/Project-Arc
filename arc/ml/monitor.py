"""E27.4 (D126): live monitoring of the shadow ML scores — pure maths + a store reader.

``arc ml monitor`` and the nightly ``ml_health`` record share :func:`health`. Every
number carries ``n``; a section below :data:`MIN_N` says ``insufficient`` instead of a
verdict. Read-only on the store; the output is a stdlib-JSON dict (E27.8 / E27.9 read it).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    import datetime as dt
    import sqlite3
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    import pandas as pd

MIN_N = 30
PSI_WARN = 0.25
PSI_BINS = 10
REL_BINS = 5
EPS = 1e-6
HEALTH_SCHEMA = 1


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson interval of a proportion (``None`` at n = 0)."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 4), round(c + h, 4))


def mean_ci(x: Sequence[float], z: float = 1.96) -> tuple[float, float] | None:
    a = np.asarray(x, dtype=float)
    if len(a) < 2:  # noqa: PLR2004
        return None
    m, se = float(a.mean()), float(a.std(ddof=1) / math.sqrt(len(a)))
    return (round(m - z * se, 4), round(m + z * se, 4))


def calibration(p: Sequence[float], y: Sequence[float], bins: int = REL_BINS) -> dict[str, Any]:
    """Reliability of a probability on closed trades: Brier vs the base rate, per bin."""
    pa, ya = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    n = len(pa)
    out: dict[str, Any] = {"n": n, "status": "ok" if n >= MIN_N else "insufficient"}
    if n == 0:
        return out
    base = float(ya.mean())
    out |= {
        "base_rate": round(base, 4),
        "base_rate_ci": wilson(float(ya.sum()), n),
        "mean_p": round(float(pa.mean()), 4),
        "brier": round(float(np.mean((pa - ya) ** 2)), 4),
        "brier_base": round(float(np.mean((base - ya) ** 2)), 4),
    }
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(pa, edges[1:-1]), 0, bins - 1)
    out["bins"] = [
        {
            "lo": round(float(edges[b]), 2),
            "hi": round(float(edges[b + 1]), 2),
            "n": int((idx == b).sum()),
            "mean_p": round(float(pa[idx == b].mean()), 4),
            "rate": round(float(ya[idx == b].mean()), 4),
            "rate_ci": wilson(float(ya[idx == b].sum()), int((idx == b).sum())),
        }
        for b in range(bins)
        if (idx == b).any()
    ]
    return out


def er_vs_realised(e_r: Sequence[float], r: Sequence[float]) -> dict[str, Any]:
    n = len(e_r)
    out: dict[str, Any] = {"n": n, "status": "ok" if n >= MIN_N else "insufficient"}
    if n:
        out |= {
            "mean_e_r": round(float(np.mean(e_r)), 4),
            "mean_r": round(float(np.mean(r)), 4),
            "mean_r_ci": mean_ci(r),
        }
    return out


def psi(
    expected: Sequence[float] | np.ndarray,
    actual: Sequence[float] | np.ndarray,
    bins: int = PSI_BINS,
) -> float | None:
    """Population stability index of *actual* vs *expected* on the expected's deciles."""
    e = np.asarray(expected, dtype=float)
    a = np.asarray(actual, dtype=float)
    e, a = e[np.isfinite(e)], a[np.isfinite(a)]
    if len(e) == 0 or len(a) == 0:
        return None
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)[1:-1]))
    pe = np.bincount(np.searchsorted(edges, e, side="right"), minlength=len(edges) + 1) / len(e)
    pa = np.bincount(np.searchsorted(edges, a, side="right"), minlength=len(edges) + 1) / len(a)
    pe, pa = np.maximum(pe, EPS), np.maximum(pa, EPS)
    return round(float(np.sum((pa - pe) * np.log(pa / pe))), 4)


def feature_drift(
    train: pd.DataFrame, live: pd.DataFrame, features: Sequence[str], top: int = 5
) -> dict[str, Any]:
    rows = []
    for f in features:
        if f in train.columns and f in live.columns:
            v = psi(train[f].to_numpy(dtype=float), live[f].to_numpy(dtype=float))
            if v is not None:
                rows.append((f, v))
    rows.sort(key=lambda t: -t[1])
    n = len(live)
    return {
        "n_live": n,
        "n_train": len(train),
        "status": "ok" if n >= MIN_N else "insufficient",
        "features": len(rows),
        "warn_above": PSI_WARN,
        "warn": [f for f, v in rows if v > PSI_WARN],
        "top": [{"feature": f, "psi": v} for f, v in rows[:top]],
    }


def score_drift(
    live: Mapping[str, Sequence[float]], ref: Mapping[str, Sequence[float]]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("e_r", "p_win"):
        lv, rv = list(live.get(k, [])), list(ref.get(k, []))
        out[k] = {
            "n_live": len(lv),
            "n_ref": len(rv),
            "status": "ok" if len(lv) >= MIN_N and len(rv) >= MIN_N else "insufficient",
            "live_mean": round(float(np.mean(lv)), 4) if lv else None,
            "ref_mean": round(float(np.mean(rv)), 4) if rv else None,
            "psi": psi(rv, lv),
        }
    return out


# ---------------------------------------------------------------------------
# Store readers
# ---------------------------------------------------------------------------


def score_entries(conn: sqlite3.Connection, since: dt.date) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'ml_scores' AND created_at >= ? "
        "ORDER BY created_at",
        (since.isoformat(),),
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def coverage(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """% of menu candidates scored, latency p95, and the reasons for ``unavailable``."""
    n = sum(int(e.get("n", 0)) for e in entries)
    ok = sum(int(e.get("n_ok", 0)) for e in entries)
    reasons: Counter[str] = Counter()
    for e in entries:
        for s in e.get("scores", []):
            if s.get("status") != "ok":
                reasons[str(s.get("reason") or e.get("reason") or "unknown")[:80]] += 1
    lat = [int(e.get("latency_ms", 0)) for e in entries]
    by_kind: Counter[str] = Counter(
        str(s.get("kind")) for e in entries for s in e.get("scores", []) if s.get("status") == "ok"
    )
    return {
        "ticks": len(entries),
        "candidates": n,
        "scored": ok,
        "pct_scored": round(100.0 * ok / n, 1) if n else None,
        "latency_ms_p95": int(np.percentile(lat, 95)) if lat else None,
        "unavailable_reasons": dict(reasons.most_common(5)),
        "scored_by_kind": dict(sorted(by_kind.items())),
    }


def closed_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Closed live trades carrying an entry score (every arm): p_win/p_stop/e_r vs truth.

    ``won`` = realised P&L > 0; ``stopped`` = exit reason ``stop``; ``r`` = realised P&L
    over the structure's max loss (the M2 label unit)."""
    q = """
        SELECT o.ml_p_win, o.ml_p_stop, o.ml_e_r, o.realised_pnl, o.exit_reason,
               json_extract(p.structure_json, '$.max_loss'), o.contracts, o.arm_id,
               json_extract(p.structure_json, '$.kind')
        FROM outcomes o JOIN proposals p ON p.proposal_hash = o.proposal_hash
        WHERE o.ml_model_version IS NOT NULL AND o.realised_pnl IS NOT NULL
          AND o.status IN ('closed', 'expired_worthless')
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outcomes)")}
    if "ml_model_version" not in cols:  # a store not yet migrated to 041
        return []
    out = []
    for pw, ps, er, pnl, reason, ml, n, arm, kind in conn.execute(q):
        pnl_f = float(pnl)
        risk = float(ml) * max(int(n or 1), 1) if ml not in (None, "") else None
        out.append(
            {
                "p_win": pw,
                "p_stop": ps,
                "e_r": er,
                "won": 1.0 if pnl_f > 0 else 0.0,
                "stopped": 1.0 if reason == "stop" else 0.0,
                "r": pnl_f / risk if risk else None,
                "arm": arm,
                "kind": kind,
            }
        )
    return out


def live_calibration(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pw = [t for t in trades if t["p_win"] is not None]
    ps = [t for t in trades if t["p_stop"] is not None]
    er = [t for t in trades if t["e_r"] is not None and t["r"] is not None]
    return {
        "p_win": calibration([t["p_win"] for t in pw], [t["won"] for t in pw]),
        "p_stop": calibration([t["p_stop"] for t in ps], [t["stopped"] for t in ps]),
        "e_r": er_vs_realised([t["e_r"] for t in er], [t["r"] for t in er]),
    }


def health(
    conn: sqlite3.Connection,
    *,
    day: dt.date,
    since: dt.date,
    drift: Mapping[str, Any] | None = None,
    parity: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``ml_health`` record (stable shape, ``schema`` bumps on change)."""
    entries = score_entries(conn, since)
    versions = sorted({str(e.get("model_version")) for e in entries if e.get("model_version")})
    live: dict[str, list[float]] = {"e_r": [], "p_win": []}
    for e in entries:
        for s in e.get("scores", []):
            for k in live:
                if s.get(k) is not None:
                    live[k].append(float(s[k]))
    return {
        "schema": HEALTH_SCHEMA,
        "day": day.isoformat(),
        "window_start": since.isoformat(),
        "model_versions": versions,
        "coverage": coverage(entries),
        "calibration": live_calibration(closed_trades(conn)),
        "feature_drift": dict(drift) if drift is not None else {"status": "not_run"},
        "parity": dict(parity) if parity is not None else {"status": "not_run"},
        "live_scores": {k: len(v) for k, v in live.items()},
        **dict(extra or {}),
    }


def health_path(data_dir: Path, day: dt.date) -> Path:
    return data_dir / "ml" / "health" / f"{day.isoformat()}.json"


def write_health(data_dir: Path, rec: Mapping[str, Any]) -> Path:
    import datetime as _dt

    p = health_path(data_dir, _dt.date.fromisoformat(str(rec["day"])))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True, default=str) + "\n")
    tmp.replace(p)
    return p


#: drift warnings listed by name in the rendered report (the rest are counted)
WARN_SHOWN = 10


def render(rec: Mapping[str, Any]) -> str:
    """Plain-text ``arc ml monitor`` output (n and CI everywhere; ``insufficient`` < 30)."""
    c = rec["coverage"]
    lines = [
        f"ML shadow monitor · {rec['window_start']}..{rec['day']} · models "
        + (", ".join(rec["model_versions"]) or "none"),
        f"coverage: {c['scored']}/{c['candidates']} candidates scored "
        f"({c['pct_scored'] if c['pct_scored'] is not None else '–'}%) over {c['ticks']} ticks; "
        f"latency p95 {c['latency_ms_p95'] if c['latency_ms_p95'] is not None else '–'} ms",
    ]
    if c["unavailable_reasons"]:
        lines.append(
            "  unavailable: " + "; ".join(f"{k} ×{v}" for k, v in c["unavailable_reasons"].items())
        )
    if c["scored_by_kind"]:
        lines.append(
            "  scored by kind: " + ", ".join(f"{k} {v}" for k, v in c["scored_by_kind"].items())
        )
    cal = rec["calibration"]
    for k in ("p_win", "p_stop"):
        s = cal[k]
        if s["status"] == "insufficient":
            lines.append(f"calibration {k}: insufficient (n={s['n']} < {MIN_N} closed trades)")
        else:
            lines.append(
                f"calibration {k}: n={s['n']} mean p {s['mean_p']} vs rate {s['base_rate']} "
                f"CI {s['base_rate_ci']}; Brier {s['brier']} vs base {s['brier_base']}"
            )
    s = cal["e_r"]
    lines.append(
        f"E[R] vs realised R: insufficient (n={s['n']} < {MIN_N})"
        if s["status"] == "insufficient"
        else f"E[R] vs realised R: n={s['n']} mean e_r {s['mean_e_r']} vs mean R "
        f"{s['mean_r']} CI {s['mean_r_ci']}"
    )
    d = rec["feature_drift"]
    if d.get("status") == "not_run":
        lines.append("feature drift: not run")
    else:
        top = ", ".join(f"{t['feature']} {t['psi']}" for t in d.get("top", []))
        warn = list(d["warn"])
        shown = ", ".join(warn[:WARN_SHOWN]) + (
            f" (+{len(warn) - WARN_SHOWN} more)" if len(warn) > WARN_SHOWN else ""
        )
        lines.append(
            f"feature drift (PSI, live n={d['n_live']} vs train n={d['n_train']}"
            f"{', insufficient' if d['status'] == 'insufficient' else ''}): top {top}; "
            f"warn >{d['warn_above']}: {len(warn)} · {shown or 'none'}"
        )
    sd = rec.get("score_drift")
    if sd:
        groups = [("all", sd.get("all", {}))] + [
            (k, v) for k, v in sorted((sd.get("by_kind") or {}).items())
        ]
        idle = [n for n, g in groups if g and all(not v["n_live"] for v in g.values())]
        if idle:  # no live scores yet: one line, not two per kind
            lines.append(
                f"score drift: no live scores yet (backtest ref n="
                f"{(sd.get('all') or {}).get('e_r', {}).get('n_ref', 0)}; {len(idle)} groups)"
            )
        for name, g in groups:
            if name in idle:
                continue
            for k, v in g.items():
                lines.append(
                    f"score drift {k} [{name}]: live n={v['n_live']} mean {v['live_mean']} "
                    f"vs backtest n={v['n_ref']} mean {v['ref_mean']} PSI {v['psi']}"
                    + (" (insufficient)" if v["status"] == "insufficient" else "")
                )
    p = rec["parity"]
    lines.append(f"parity: {p.get('status')}" + (f" · {p['detail']}" if p.get("detail") else ""))
    lr = rec.get("last_retrain")
    if lr:
        lines.append(
            f"last retrain: {lr.get('version')} {'pass' if lr.get('passed') else 'fail'} on "
            f"holdout {lr.get('holdout_start')}..{lr.get('holdout_end')} (n={lr.get('n')}, "
            f"gap {lr.get('gap_days')}d) · candidate only"
        )
    return "\n".join(lines) + "\n"
