"""Monthly ``ml.retrain`` with a rolling holdout (E27.4, D140).

The owner's spec (2026-10-27, card comment 788), per run:

* **Ledger extension** — the candidate ledger is extended to the newest session the
  history cache holds (``history.topup``): the done markers of the current (and, in
  January, the previous) year are cleared and those partitions rebuilt through
  :func:`arc.ml.ledger.build.build_ledger` (measured: ~106 s per ticker-year with one
  worker, so ~2.5 h for C500 at 6 workers; it yields to the E26.3 study window and
  resumes next run). Rows whose label hasn't settled stay label-less (never trained on).
* **Holdout** — the last :data:`HOLDOUT_MONTHS` *completed* calendar months: months
  that end at least :func:`gap_days` before the ledger's data end, so every candidate
  in them has its outcome resolved. It slides forward one month per run.
* **Embargo gap** — the longest observed label span (decision date → ``label_end_date``)
  in the ledger, floored at :data:`MIN_GAP_DAYS`: no training label may end inside the
  gap before the holdout starts.
* **Train window** — expanding: 2020-01-01 → holdout start − gap (labels ending by
  then). It gains a month per run; 2024+ rows outside the holdout are training data.
* **Gate** — the new candidate **and** the serving incumbent are scored on the same
  holdout (neither trained on it). Pass/fail only; nothing is tuned against it (the
  E27.3 config and walk-forward folds inside the train window pick hyper-parameters).
  Every slice (split × kind / sleeve) reports n; n < :data:`MIN_N` is
  ``insufficient`` and never decides the gate.
* **Registry** — the result is registered ``candidate`` only (no D133 auto-shadow);
  promotion stays ``arc ml promote`` by the owner. Each run is logged to
  ``holdout_reads.jsonl`` as a ``kind: rolling`` read (reason ``ml.retrain monthly
  rolling``) with its window, gap, n and metrics; the E27.3 one-time reads stay the
  clean record of the original versions.

Context only: no gate, sizing, ranking or prompt reads any of this.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import shutil
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    import numpy as np
    import pandas as pd

    from arc.ml.m2.config import M2Config

log = structlog.get_logger(__name__)

REASON = "ml.retrain monthly rolling"
HOLDOUT_MONTHS = 6
#: the embargo never drops below this (the owner's ~45–60 day range)
MIN_GAP_DAYS = 45
#: slices with fewer settled rows than this are "insufficient" (never decide the gate)
MIN_N = 30
#: the walk-forward folds inside the rolling train window (the last N 12-month blocks
#: that end at the train end)
N_FOLDS = 2
#: the NW t-stat threshold on the holdout e_r daily IC (same as the E27.3 rule)
IC_T_MIN = 2.0
STATE_FILE = "_extend.json"
#: candidate dates stop this many days before the data end, so every row's label has
#: settled (the E27.3 build: end 2026-08-14 for data_end 2026-10-15)
LABEL_ROOM_DAYS = 62


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollingWindow:
    """One run's train / gap / holdout dates (all inclusive)."""

    data_end: dt.date
    gap_days: int
    holdout_start: dt.date
    holdout_end: dt.date
    train_start: dt.date
    train_end: dt.date

    def to_json(self) -> dict[str, Any]:
        return {
            "data_end": self.data_end.isoformat(),
            "gap_days": self.gap_days,
            "holdout_start": self.holdout_start.isoformat(),
            "holdout_end": self.holdout_end.isoformat(),
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
        }


def _month_start(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _add_months(d: dt.date, n: int) -> dt.date:
    m = d.year * 12 + (d.month - 1) + n
    return dt.date(m // 12, m % 12 + 1, 1)


def plan_window(
    data_end: dt.date,
    gap_days: int,
    *,
    months: int = HOLDOUT_MONTHS,
    train_start: dt.date = dt.date(2020, 1, 1),
    last_decision: dt.date | None = None,
) -> RollingWindow:
    """The rolling window for a ledger whose labels are resolved through *data_end*.

    A month is *completed* when it ends at least *gap_days* before *data_end* (every
    candidate dated in it has had the longest holding period to resolve). The holdout
    is the last *months* such months; training labels end before ``holdout_start −
    gap_days``.
    """
    if gap_days < 1 or months < 1:
        msg = f"gap_days and months must be ≥ 1 (got {gap_days}, {months})"
        raise ValueError(msg)
    resolved = data_end - dt.timedelta(days=gap_days)
    if last_decision is not None:
        resolved = min(resolved, last_decision)
    month_end = _add_months(_month_start(resolved), 1) - dt.timedelta(days=1)
    last_full = (
        _month_start(resolved) if resolved == month_end else _add_months(_month_start(resolved), -1)
    )
    hold_end = _add_months(last_full, 1) - dt.timedelta(days=1)
    hold_start = _add_months(last_full, -(months - 1))
    train_end = hold_start - dt.timedelta(days=gap_days + 1)
    if train_end <= train_start:
        msg = f"rolling window leaves no training data (train end {train_end})"
        raise ValueError(msg)
    return RollingWindow(
        data_end=data_end,
        gap_days=gap_days,
        holdout_start=hold_start,
        holdout_end=hold_end,
        train_start=train_start,
        train_end=train_end,
    )


def rolling_folds(window: RollingWindow, n: int = N_FOLDS) -> list[Any]:
    """Walk-forward test blocks inside the train window: the last *n* 12-month blocks
    that end at the train end (purged the E27.3 way by :class:`arc.ml.validate.Fold`)."""
    from arc.ml import validate as v

    folds = []
    end = window.train_end
    for _ in range(n):
        start = _add_months(_month_start(end), -11)
        if start <= window.train_start:
            break
        folds.append(v.Fold(f"test{start:%Y%m}", start, end))
        end = start - dt.timedelta(days=1)
    if not folds:
        msg = "rolling window too short for a walk-forward fold"
        raise ValueError(msg)
    return list(reversed(folds))


def m1_folds(window: RollingWindow, n: int = N_FOLDS) -> list[Any]:
    """The same blocks as :func:`rolling_folds`, as M1 fold plans."""
    from arc.ml.m1.train import FoldPlan

    return [FoldPlan(f.name, f.test_start, f.test_end) for f in rolling_folds(window, n)]


def gap_days(ledger_root: Path, *, years: Sequence[int], floor: int = MIN_GAP_DAYS) -> int:
    """The longest decision → ``label_end_date`` span (calendar days) among settled rows
    of *years* in the ledger, floored at *floor* (the embargo)."""
    import pandas as pd
    import pyarrow.parquet as pq

    from arc.ml.ledger.build import version_dir

    vdir = ledger_root if (ledger_root / "manifest.json").exists() else version_dir(ledger_root)
    span = 0
    for y in years:
        for f in sorted(vdir.glob(f"profile=*/year={y}/part-*.parquet")):
            pf = pq.ParquetFile(f)  # a single file: no hive-partition inference
            if not {"date", "label_end_date"} <= set(pf.schema_arrow.names):
                continue
            t = pf.read(columns=["date", "label_end_date"]).to_pandas()
            if t.empty:
                continue
            d = (pd.to_datetime(t["label_end_date"]) - pd.to_datetime(t["date"])).dt.days
            if d.notna().any():
                span = max(span, int(d.max()))
    return max(span, floor)


def ledger_dates(ledger_root: Path) -> tuple[dt.date, dt.date]:
    """``(end, data_end)`` of the ledger manifest: last candidate date, label data end."""
    from arc.ml.ledger.build import version_dir

    vdir = ledger_root if (ledger_root / "manifest.json").exists() else version_dir(ledger_root)
    man = json.loads((vdir / "manifest.json").read_text())
    return (
        dt.date.fromisoformat(str(man["end"])[:10]),
        dt.date.fromisoformat(str(man["data_end"])[:10]),
    )


def ledger_data_end(ledger_root: Path) -> dt.date:
    return ledger_dates(ledger_root)[1]


# ---------------------------------------------------------------------------
# Ledger extension
# ---------------------------------------------------------------------------


def _ledger_tickers(vdir: Path) -> list[str]:
    """Every ticker the ledger ever finished (done markers of any year)."""
    return sorted({p.name for p in vdir.glob("_done/*/*")})


def extension_years(end: dt.date, horizon_days: int = 120) -> list[int]:
    """Years whose partitions can still gain rows or settle labels by *end*."""
    return sorted({end.year, (end - dt.timedelta(days=horizon_days)).year})


def pinned_extension(ledger_root: Path) -> dt.date | None:
    """The ``data_end`` of an extension still in progress (resumes use it, not a newer
    cache end, so a multi-night rebuild converges)."""
    from arc.ml.ledger.build import version_dir

    sp = version_dir(ledger_root) / STATE_FILE
    if not sp.is_file():
        return None
    return dt.date.fromisoformat(json.loads(sp.read_text())["end"])


def reset_markers(ledger_root: Path, years: Sequence[int], end: dt.date) -> int:
    """Start an extension to *end*: clear the done markers of *years* once (a resumed
    extension to the same *end* keeps the markers of finished tickers)."""
    from arc.ml.ledger.build import version_dir

    vdir = version_dir(ledger_root)
    sp = vdir / STATE_FILE
    if sp.is_file() and json.loads(sp.read_text()).get("end") == end.isoformat():
        return 0
    n = 0
    for y in years:
        d = vdir / "_done" / str(y)
        if d.is_dir():
            n += sum(1 for _ in d.iterdir())
            shutil.rmtree(d)
    sp.write_text(json.dumps({"end": end.isoformat(), "years": list(years)}) + "\n")
    return n


def extend_ledger(
    data_dir: Path,
    *,
    data_end: dt.date,
    ranking_file: Path,
    universe: str = "c500",
    workers: int = 6,
    tickers: Sequence[str] | None = None,
    stop: Callable[[], bool] | None = None,
    say: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """Rebuild the open years of the ledger with labels through *data_end* (the history
    cache's last session) and candidates through ``data_end − LABEL_ROOM_DAYS``
    (resumable). Returns ``{"end", "data_end", "years", "cleared", "pending", "complete"}``."""
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config
    from arc.ml.ledger.build import build_ledger, pending, version_dir
    from arc.routines.config import RangeEdgesSettings
    from arc.routines.strategy import load_strategy_data

    ledger_root = data_dir / "ml" / "ledger"
    vdir = version_dir(ledger_root)
    man = json.loads((vdir / "manifest.json").read_text())
    data_end = pinned_extension(ledger_root) or data_end
    end = data_end - dt.timedelta(days=LABEL_ROOM_DAYS)
    years = extension_years(data_end)
    known = _ledger_tickers(vdir)  # read before the markers of open years are cleared
    cleared = reset_markers(ledger_root, years, data_end)
    edges = RangeEdgesSettings.model_validate(load_strategy_data(None).get("range_edges") or {})
    res = build_ledger(
        data_dir=data_dir,
        out_root=ledger_root,
        universe=universe,
        start=dt.date.fromisoformat(str(man.get("start", "2020-01-01"))[:10]),
        end=end,
        data_end=data_end,
        profiles=list(man.get("profiles") or ["margin", "cash_debit", "cash_long_only"]),
        cfg=load_ranking_file(ranking_file),
        cost=load_cost_model(),
        exits=load_exit_config(),
        settings=ArcSettings(),
        tickers=tickers,
        workers=workers,
        cache_dir=data_dir,
        range_rule=edges.rule() if edges.enabled else None,
        progress=say,
        stop=stop,
    )
    want = list(tickers) if tickers else known
    left = len(pending(vdir, sorted(set(want)), years))
    complete = left == 0
    if complete:
        (vdir / STATE_FILE).unlink(missing_ok=True)
    return {
        "end": end.isoformat(),
        "data_end": data_end.isoformat(),
        "years": years,
        "cleared": cleared,
        "jobs": res.jobs,
        "pending": left,
        "complete": complete,
    }


# ---------------------------------------------------------------------------
# Holdout gate
# ---------------------------------------------------------------------------


def _slice_metrics(
    d: pd.DataFrame, s: pd.DataFrame, labels: dict[str, str], w: np.ndarray, m: np.ndarray
) -> dict[str, Any]:
    import numpy as np

    from arc.ml import validate as v

    n = int(m.sum())
    out: dict[str, Any] = {"n": n, "status": "ok" if n >= MIN_N else "insufficient"}
    if n == 0:
        return out
    yw = d[labels["win"]].astype(float).to_numpy()[m]
    yr = d[labels["r"]].astype(float).to_numpy()[m]
    pw = s["p_win"].to_numpy(dtype=float)[m]
    out["n_eff"] = round(v.kish_n_eff(w[m]), 1)
    out["win_rate"] = round(float(np.average(yw, weights=w[m])), 4)
    out["win_log_loss"] = round(v.log_loss(yw, pw, w[m]), 5)
    out["win_brier"] = round(v.brier(yw, pw, w[m]), 5)
    if "managed_pop" in d:
        mp = np.clip(
            np.nan_to_num(d["managed_pop"].astype(float).to_numpy()[m], nan=0.5), 1e-4, 1 - 1e-4
        )
        out["win_log_loss_managed_pop"] = round(v.log_loss(yw, mp, w[m]), 5)
    dates = [x for x, k in zip(d["date"].tolist(), m, strict=True) if k]
    rk = v.rank_metrics(dates, s["e_r"].to_numpy(dtype=float)[m], yr)
    out["er_ic_mean"] = None if math.isnan(rk["ic_mean"]) else round(rk["ic_mean"], 4)
    out["er_ic_t"] = None if math.isnan(rk["ic_nw_t"]) else round(rk["ic_nw_t"], 2)
    out["er_ic_days"] = int(rk["ic_days"])
    out["er_mean_pred"] = round(
        float(np.average(s["e_r"].to_numpy(dtype=float)[m], weights=w[m])), 4
    )
    out["r_mean_real"] = round(float(np.average(yr, weights=w[m])), 4)
    return out


def holdout_table(
    rows: pd.DataFrame,
    scores: dict[str, pd.DataFrame],
    labels: dict[str, str],
    w: np.ndarray,
) -> list[dict[str, Any]]:
    """One record per model × slice (split × all / kind / sleeve) with n and metrics."""
    from arc.ml import validate as v

    out = []
    for lab, m in v.group_slices(rows):
        for model, s in scores.items():
            out.append({"model": model, **lab, **_slice_metrics(rows, s, labels, w, m)})
    return out


def gate(table: Sequence[dict[str, Any]], *, incumbent: bool) -> dict[str, Any]:
    """Pass/fail on the All/all slice (decided only by a sufficient slice).

    The candidate must beat the managed-PoP baseline on win log-loss, have a positive
    holdout e_r IC with Newey–West t > :data:`IC_T_MIN`, and — when an incumbent was
    scored — be no worse than it on both log-loss and IC.
    """

    def _all(model: str) -> dict[str, Any] | None:
        return next(
            (
                r
                for r in table
                if r["model"] == model and r["split"] == "All" and r["group"] == "all"
            ),
            None,
        )

    c = _all("candidate")
    if c is None or c["status"] != "ok":
        return {
            "passed": False,
            "status": "insufficient",
            "detail": f"holdout n={0 if c is None else c['n']} < {MIN_N}",
        }
    legs: dict[str, bool] = {
        "beats_managed_pop": c.get("win_log_loss_managed_pop") is not None
        and c["win_log_loss"] < c["win_log_loss_managed_pop"],
        "ic_pass": (c.get("er_ic_mean") or 0.0) > 0 and (c.get("er_ic_t") or 0.0) > IC_T_MIN,
    }
    i = _all("incumbent") if incumbent else None
    if i is not None and i["status"] == "ok":
        legs["ll_not_worse_than_incumbent"] = c["win_log_loss"] <= i["win_log_loss"]
        legs["ic_not_worse_than_incumbent"] = (c.get("er_ic_mean") or 0.0) >= (
            i.get("er_ic_mean") or 0.0
        )
    passed = all(legs.values())
    return {"passed": passed, "status": "pass" if passed else "fail", "legs": legs}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass
class FamilyResult:
    family: str
    version: str | None = None
    incumbent: str | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    n_train: int = 0
    n_holdout: int = 0
    error: str | None = None

    def line(self) -> str:
        if self.error:
            return f"{self.family}: error ({self.error[:80]})"
        g = self.gate.get("status", "?")
        inc = f" vs {self.incumbent}" if self.incumbent else ""
        return f"{self.family} {self.version} {g}{inc} (holdout n {self.n_holdout})"


@dataclass
class RetrainResult:
    window: RollingWindow | None = None
    ledger: dict[str, Any] = field(default_factory=dict)
    m1_version: str | None = None
    m1_error: str | None = None
    families: dict[str, FamilyResult] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        if self.window is None:
            return "ml.retrain: no window"
        w = self.window
        fam = "; ".join(f.line() for f in self.families.values())
        if self.m1_error:
            fam += f" · M1 retrain failed ({self.m1_error[:80]})"
        return (
            f"ml.retrain: train ≤ {w.train_end} · gap {w.gap_days}d · holdout "
            f"{w.holdout_start:%Y-%m}..{w.holdout_end:%Y-%m} · {fam} · candidate only, "
            "promote is the owner's `arc ml promote`"
        )

    def metrics(self) -> dict[str, Any]:
        return {
            "window": self.window.to_json() if self.window else None,
            "ledger": self.ledger,
            "m1_version": self.m1_version,
            "m1_error": self.m1_error,
            "families": {
                k: {
                    "version": f.version,
                    "incumbent": f.incumbent,
                    "gate": f.gate.get("status"),
                    "n_train": f.n_train,
                    "n_holdout": f.n_holdout,
                    "error": f.error,
                }
                for k, f in self.families.items()
            },
            "seconds": dict(self.seconds),
        }


def _incumbent(models_root: Path, family: str, window: RollingWindow) -> tuple[str | None, Any]:
    """The serving version (champion > shadow > candidate) when it never trained on the
    holdout; else ``(None, reason)``."""
    from arc.ml.m2.predict import load_model
    from arc.ml.serve import pick_version

    try:
        version = pick_version(models_root, family)
    except Exception as e:  # noqa: BLE001 - no incumbent is a valid state
        return None, str(e)
    model = load_model(models_root, family, version)
    tw = model.manifest.get("train_window") or {}
    end = dt.date.fromisoformat(str(tw.get("end", "2023-12-31"))[:10])
    if end >= window.holdout_start:
        return None, f"{version} trained through {end}, inside the holdout"
    return version, model


def evaluate_family(
    family: str,
    cfg: M2Config,
    window: RollingWindow,
    *,
    models_root: Path,
    ledger_root: Path,
    features_root: Path,
    candidate: str,
    incumbent: tuple[str | None, Any],
    tickers: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """Score the candidate and the incumbent on the settled holdout rows."""
    from arc.ml import validate as v
    from arc.ml.m2 import dataset as ds
    from arc.ml.m2.pipeline import scored_rows
    from arc.ml.m2.predict import load_model

    model = load_model(models_root, family, candidate)
    years = list(range(window.holdout_start.year, window.holdout_end.year + 1))
    rows, s = scored_rows(
        model,
        ledger_root,
        features_root,
        family,
        years,
        cfg=cfg,
        split="all",
        reason=REASON,
        passes_filters=False,
        cap_per_day=cfg.cap_per_day,
        tickers=tickers,
    )
    keep = (
        (rows["date"] >= window.holdout_start)
        & (rows["date"] <= window.holdout_end)
        & (rows["label_end_date"] <= window.data_end)
    ).to_numpy()
    rows = rows[keep].reset_index(drop=True)
    scores = {"candidate": s[keep].reset_index(drop=True)}
    inc_version, inc = incumbent
    if inc_version is not None:
        scores["incumbent"] = inc.score(rows).reset_index(drop=True)
    labels = ds.label_cols(cfg.label_x)
    w = v.sample_weights(rows, rows["inv_keep"].to_numpy(dtype=float))
    table = holdout_table(rows, scores, labels, w)
    return table, gate(table, incumbent=inc_version is not None), len(rows)


def run_retrain(
    *,
    data_dir: Path,
    now: dt.datetime,
    code_sha: str,
    families: Sequence[str] = ("credit", "debit"),
    cfg: M2Config | None = None,
    data_end: dt.date | None = None,
    retrain_m1: bool = True,
    tickers: Sequence[str] | None = None,
    months: int = HOLDOUT_MONTHS,
    min_gap_days: int = MIN_GAP_DAYS,
    monotone_check: bool = True,
    say: Callable[[str], None] = lambda _m: None,
    clock: Callable[[], float] = time.monotonic,
) -> RetrainResult:
    """Train M1 + M2 on the rolling window, gate each family on its holdout, register
    the results as ``candidate`` and log the rolling read (see the module doc). The
    ledger must already be extended (:func:`extend_ledger`)."""
    from arc.ml import registry as reg
    from arc.ml.m2.config import load_config
    from arc.ml.m2.pipeline import finalize_manifest
    from arc.ml.m2.train import train_family

    cfg = cfg or load_config()
    ledger_root = data_dir / "ml" / "ledger"
    features_root = data_dir / "ml" / "features"
    models_root = data_dir / "ml" / "models"
    res = RetrainResult()
    last, end = ledger_dates(ledger_root)
    end = data_end or end
    years_seen = sorted({end.year, end.year - 1})
    gap = gap_days(ledger_root, years=years_seen, floor=min_gap_days)
    window = plan_window(end, gap, months=months, last_decision=last)
    res.window = window
    say(f"rolling window {window.to_json()}")

    m1_version = None
    if retrain_m1:
        t0 = clock()
        from arc.ml.m1.train import train_m1

        try:
            run = train_m1(
                data_dir,
                features_root,
                models_root / "m1",
                code_sha=code_sha,
                tickers=tickers,
                dev_end=window.train_end,
                folds=m1_folds(window),
                holdout_reason=REASON,
                progress=say,
            )
            m1_version = run.manifest.version
        except Exception as exc:  # noqa: BLE001 - M2 still trains on the newest M1
            res.m1_error = f"{type(exc).__name__}: {exc}"[:300]
            say(f"M1 retrain failed ({res.m1_error}); M2 uses the newest M1")
        res.m1_version = m1_version
        res.seconds["m1"] = round(clock() - t0, 1)

    folds = rolling_folds(window)
    years = list(range(window.train_start.year, window.train_end.year + 1))
    for family in families:
        fres = FamilyResult(family)
        res.families[family] = fres
        t0 = clock()
        try:
            inc = _incumbent(models_root, family, window)
            run = train_family(
                family,
                cfg,
                ledger_root=ledger_root,
                features_root=features_root,
                out_root=models_root,
                code_sha=code_sha,
                now=now,
                tickers=tickers,
                folds=folds,
                monotone_check=monotone_check,
                progress=say,
                years=years,
                train_end=window.train_end,
                holdout_reason=REASON,
                m1_version=m1_version,
            )
            fres.version = run.version
            fres.incumbent = inc[0]
            fres.n_train = int(
                json.loads((run.vdir / "manifest.json").read_text()).get("n_rows") or 0
            )
            table, g, n = evaluate_family(
                family,
                cfg,
                window,
                models_root=models_root,
                ledger_root=ledger_root,
                features_root=features_root,
                candidate=run.version,
                incumbent=inc,
                tickers=tickers,
            )
            fres.gate, fres.n_holdout = g, n
            if inc[0] is None:
                fres.gate["incumbent_note"] = str(inc[1])
            (run.vdir / "rolling_holdout.json").write_text(
                json.dumps(
                    {"window": window.to_json(), "gate": g, "slices": table},
                    indent=1,
                    default=str,
                    sort_keys=True,
                )
                + "\n"
            )
            man = finalize_manifest(
                run.vdir,
                {
                    "rule_passed": bool(g.get("passed")),
                    "replayed": False,
                    "retrain": {"kind": "rolling", "reason": REASON, **window.to_json()},
                },
            )
            reg.register(
                models_root,
                family,
                run.version,
                now=now,
                rule="m2_rolling_holdout",
                rule_passed=bool(g.get("passed")),
                feature_schema=man["feature_schema"],
                summary={
                    "rolling_holdout": g.get("status"),
                    "holdout": f"{window.holdout_start}..{window.holdout_end}",
                    "holdout_n": n,
                    "incumbent": inc[0],
                },
                auto_shadow=False,
            )
            all_rows = [r for r in table if r["split"] == "All" and r["group"] == "all"]
            reg.log_rolling_read(
                models_root,
                family,
                {
                    "at": now.isoformat(),
                    "version": run.version,
                    "incumbent": inc[0],
                    "reason": REASON,
                    "code_sha": code_sha,
                    **window.to_json(),
                    "n": n,
                    "passed": bool(g.get("passed")),
                    "gate": g,
                    "metrics": all_rows,
                    "slices_insufficient": sum(
                        1 for r in table if r["model"] == "candidate" and r["status"] != "ok"
                    ),
                },
            )
        except Exception as e:  # noqa: BLE001 - one family failing leaves the other
            fres.error = f"{type(e).__name__}: {e}"[:300]
            log.warning("ml.retrain.family_failed", family=family, error=fres.error, exc_info=True)
        res.seconds[family] = round(clock() - t0, 1)
    log.info("ml.retrain", **res.metrics())
    return res


@dataclass
class ChildRetrain:
    """:func:`run_retrain` as reported by the child process (:func:`retrain_in_child`)."""

    text: str
    data: dict[str, Any]

    def summary(self) -> str:
        return self.text

    def metrics(self) -> dict[str, Any]:
        return self.data

    def errors(self) -> dict[str, str]:
        fams = self.data.get("families") or {}
        return {k: str(v["error"]) for k, v in fams.items() if v.get("error")}


def retrain_in_child(
    *,
    repo: Path,
    data_dir: Path,
    code_sha: str,
    families: Sequence[str],
    months: int,
    min_gap_days: int,
    env: dict[str, str],
    timeout_s: float,
    runner: Callable[..., Any] | None = None,
) -> ChildRetrain:
    """Run ``arc ml retrain --json`` in a lower-priority child process whose environment
    reaches ``libomp`` (:func:`arc.ml.m2.train.lightgbm_env`; the routines tick's own
    process can't change its dyld path), and return its report. The priority drop is
    ``os.nice`` in the child, not ``/usr/bin/nice``: macOS strips ``DYLD_*`` when it
    executes a protected system binary."""
    import os
    import subprocess
    import sys

    from arc.ml.m2.train import lightgbm_env

    cmd = [
        sys.executable, "-m", "arc.cli", "ml", "retrain",
        "--data", str(data_dir), "--families", ",".join(families), "--json",
        "--holdout-months", str(months), "--min-gap-days", str(min_gap_days),
        "--code-sha", code_sha,
    ]  # fmt: skip
    proc = (runner or subprocess.run)(
        cmd,
        cwd=repo,
        env=lightgbm_env(env),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        preexec_fn=lambda: os.nice(10),  # noqa: PLW1509 - no threads touched before exec
    )
    line = next(
        (ln for ln in reversed((proc.stdout or "").splitlines()) if ln.startswith("{")), None
    )
    if line is None:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        msg = f"ml retrain exited {proc.returncode} without a report: {' | '.join(tail)}"
        raise RuntimeError(msg)
    out = json.loads(line)
    return ChildRetrain(text=str(out["summary"]), data=dict(out["metrics"]))
