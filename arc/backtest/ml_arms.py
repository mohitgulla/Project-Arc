"""ML arm options in the portfolio sim (card E27.5, PLAN D126).

The backtest reads a model only through :func:`arc.ml.registry.model_asof`: on session
*d* it uses the walk-forward model trained on labels that ended before *d* (purge +
embargo), never a champion pointer and never a model trained on *d*'s own year.

* **M2** (``ml_ranker: ml_er``, ``ml_filter``, ``ml_size``): the served fold model scores
  the E7.21 candidate-ledger rows of its sessions (the ledger is built from the same sim
  menus, so ``(day, ticker, key)`` joins them; the join coverage is reported). The walk
  then re-keys / drops / resizes the pool through :func:`arc.backtest.sim.run_sim`'s
  ``key_override`` and ``size_override`` hooks (:class:`MlHooks`).
* **M1** (``exits.model.path_vol: m1`` Monte Carlo paths, the ``m1_direction`` stance
  proxy): the served fold model's published outputs for its test year
  (``oos_m1_features.parquet``, written by E27.2 from exactly that fold model).

Sessions with no servable model (before the first walk-forward test year) carry no
score: ``ml_er`` drops their candidates, ``ml_filter`` and ``ml_size`` leave them as the
control arm has them, the M1 path source falls back to ``realized_forecast`` and the
``m1_direction`` proxy reads ``unknown`` (the build's unlabelled-session rule: the whole
menu without a stance filter, nothing with one). ML arms never score holdout days
(D84/D126: only E27.3's single logged read does).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from arc.backtest.ml_config import MlFilter, MlSize
    from arc.backtest.sim import SimCandidate

log = structlog.get_logger()

#: Account profile → M2 family (E27.3 ``FAMILY_PROFILE``, inverted).
PROFILE_FAMILY: dict[str, str] = {"margin": "credit", "cash_debit": "debit"}
#: First holdout session (D84): ML arms never score on or after it.
HOLDOUT_START = dt.date(2024, 1, 1)

ScoreKey = tuple[dt.date, str, str]


class MlArmError(RuntimeError):
    """An ML arm option that cannot run (no family for the profile, holdout window)."""


# ---------------------------------------------------------------------------
# Pure rules (ranker key, filter, size)
# ---------------------------------------------------------------------------


def ml_er_key(k: tuple[Any, ...], e_r: float | None) -> tuple[Any, ...] | None:
    """The ``ml_er`` sort key: ``(0, −e_r, incumbent tie, key)``; None (drop) when the
    candidate has no finite score."""
    if e_r is None or not math.isfinite(e_r):
        return None
    return (0, -e_r, k[2], k[3])


def passes_ml_filter(e_r: float | None, p_stop: float | None, f: MlFilter) -> bool:
    """``ml_filter``: keep iff ``e_r >= min_e_r`` and ``p_stop <= max_p_stop`` (a null
    leg is off). An unscored candidate passes (the filter only removes what it scored)."""
    if e_r is None or p_stop is None:
        return True
    if f.min_e_r is not None and not e_r >= f.min_e_r:
        return False
    return not (f.max_p_stop is not None and not p_stop <= f.max_p_stop)


def size_scale(e_r: float | None, median_pos: float | None, s: MlSize) -> float:
    """``clip(e_r / median_positive_e_r, floor, cap)``; 1.0 when off or unscored."""
    if s.mode == "off" or e_r is None or not math.isfinite(e_r):
        return 1.0
    if median_pos is None or not median_pos > 0:
        return 1.0
    return min(max(e_r / median_pos, s.floor), s.cap)


def shrink_contracts(n: int, scale: float) -> int:
    """D18 contracts *n* scaled down: ``max(1, floor(n × scale))``, never above *n*.

    One contract stays one: shrinking sizes, it never removes a trade (that is
    ``ml_filter``'s job).
    """
    if n <= 0:
        return n
    return min(n, max(1, math.floor(n * scale + 1e-9)))


def median_positive(values: Sequence[float]) -> float | None:
    pos = sorted(v for v in values if math.isfinite(v) and v > 0)
    if not pos:
        return None
    m = len(pos) // 2
    return pos[m] if len(pos) % 2 else 0.5 * (pos[m - 1] + pos[m])


# ---------------------------------------------------------------------------
# M2 scores through model_asof
# ---------------------------------------------------------------------------


@dataclass
class MlScores:
    """Point-in-time M2 scores of one profile: ``(day, ticker, key) → (e_r, p_stop)``."""

    family: str
    scores: dict[ScoreKey, tuple[float, float]] = field(default_factory=dict)
    #: session → median of that session's positive e_r (the ``ml_size`` reference).
    median_pos: dict[dt.date, float | None] = field(default_factory=dict)
    #: sessions a model served (others: no model before the first test year).
    served: set[dt.date] = field(default_factory=set)
    #: model (fold path) → sessions it served, for the report.
    models: dict[str, list[dt.date]] = field(default_factory=dict)

    def add(self, rows: pd.DataFrame, s: pd.DataFrame) -> None:
        by_day: dict[dt.date, list[float]] = {}
        for d, t, k, er, ps in zip(
            rows["date"], rows["ticker"], rows["key"], s["e_r"], s["p_stop"], strict=True
        ):
            self.scores[(d, str(t), str(k))] = (float(er), float(ps))
            by_day.setdefault(d, []).append(float(er))
        for d, ers in by_day.items():
            self.median_pos[d] = median_positive(ers)


def family_for(profile: str) -> str:
    fam = PROFILE_FAMILY.get(profile)
    if fam is None:
        msg = f"ML arms: no M2 family for profile {profile!r} (one of {sorted(PROFILE_FAMILY)})"
        raise MlArmError(msg)
    return fam


def _calendar(sessions: Sequence[dt.date]) -> list[dt.date]:
    from arc.utils.calendar import sessions_between

    hi = max(max(sessions, default=HOLDOUT_START), HOLDOUT_START)
    return sessions_between(dt.date(2019, 1, 1), hi)


def serving_plan(
    models_root: Path, family: str, sessions: Sequence[dt.date], version: str | None
) -> dict[str, tuple[Any, list[dt.date]]]:
    """model path → (its :class:`ModelAsOf`, the sessions it serves) via ``model_asof``."""
    from arc.ml.registry import asof_table, model_asof

    cal = _calendar(sessions)
    table = asof_table(models_root, family, cal, version)
    plan: dict[str, tuple[Any, list[dt.date]]] = {}
    for d in sorted(set(sessions)):
        if d >= HOLDOUT_START:
            continue
        m = model_asof(models_root, family, d, table=table)
        if m is None:
            continue
        if not m.labels_before <= d:  # model_asof's contract; re-checked at the use site
            msg = f"{family}: model {m.path} trained on labels up to {m.labels_before} > {d}"
            raise MlArmError(msg)
        plan.setdefault(m.path, (m, []))[1].append(d)
    return plan


def load_m2_scores(
    *,
    ml_root: Path,
    profile: str,
    sessions: Sequence[dt.date],
    tickers: Sequence[str] | None,
    version: str | None = None,
) -> MlScores:
    """Score the ledger rows of *sessions* with the model each session may use.

    *ml_root* is ``<data>/ml`` (``models/``, ``ledger/``, ``features/``).
    """
    from pathlib import Path as _Path

    from arc.ml.m2.config import M2Config
    from arc.ml.m2.pipeline import scored_rows
    from arc.ml.m2.predict import load_dir

    family = family_for(profile)
    out = MlScores(family=family)
    plan = serving_plan(ml_root / "models", family, sessions, version)
    for path, (m, days) in sorted(plan.items()):
        model = load_dir(_Path(path))
        vman = json.loads((ml_root / "models" / family / m.version / "manifest.json").read_text())
        cfg = M2Config.model_validate(vman["config"]) if "config" in vman else M2Config()
        keep = set(days)
        years = sorted({d.year for d in days})
        rows, s = scored_rows(
            model,
            ml_root / "ledger",
            ml_root / "features",
            family,
            years,
            cfg=cfg,
            tickers=None if tickers is None else list(tickers),
        )
        m = rows["date"].isin(keep).to_numpy()
        out.add(rows[m].reset_index(drop=True), s[m].reset_index(drop=True))
        out.served |= keep
        out.models[path] = sorted(days)
        log.info("backtest.ml.m2_scores", family=family, model=path, sessions=len(days))
    return out


class MlHooks:
    """``key_override`` + ``size_override`` for one profile's walk; counts score lookups."""

    def __init__(self, scores: MlScores, *, ranker: str, flt: MlFilter, size: MlSize) -> None:
        self.s = scores
        self.ranker = ranker
        self.flt = flt
        self.size = size
        self.hit = 0
        self.miss = 0
        self.dropped = 0
        self.resized = 0

    def _get(self, sc: SimCandidate) -> tuple[float, float] | None:
        c = sc.candidate
        return self.s.scores.get((c.day, c.underlying, c.inputs.key))

    def key_override(self, sc: SimCandidate, k: tuple[Any, ...]) -> tuple[Any, ...] | None:
        day = sc.candidate.day
        if day not in self.s.served:  # no model for this session: the control walk
            return k
        got = self._get(sc)
        if got is None:
            self.miss += 1
        else:
            self.hit += 1
        er, ps = got if got is not None else (None, None)
        if not passes_ml_filter(er, ps, self.flt):
            self.dropped += 1
            return None
        if self.ranker == "ml_er":
            out = ml_er_key(k, er)
            if out is None:
                self.dropped += 1
            return out
        return k

    def size_override(self, sc: SimCandidate, n: int) -> int:
        if self.size.mode == "off" or sc.candidate.day not in self.s.served:
            return n
        got = self._get(sc)
        er = None if got is None else got[0]
        scale = size_scale(er, self.s.median_pos.get(sc.candidate.day), self.size)
        m = shrink_contracts(n, scale)
        if m != n:
            self.resized += 1
        return m

    def coverage(self) -> float | None:
        n = self.hit + self.miss
        return self.hit / n if n else None

    def summary(self) -> dict[str, Any]:
        cov = self.coverage()
        return {
            "family": self.s.family,
            "sessions_served": len(self.s.served),
            "models": {p: [str(min(d)), str(max(d))] for p, d in self.s.models.items()},
            "lookups": self.hit + self.miss,
            "coverage": None if cov is None else round(cov, 4),
            "dropped": self.dropped,
            "resized": self.resized,
        }


# ---------------------------------------------------------------------------
# M1 outputs through model_asof
# ---------------------------------------------------------------------------


def load_m1_outputs(
    *,
    ml_root: Path,
    tickers: Sequence[str],
    sessions: Sequence[dt.date],
    version: str | None = None,
) -> dict[str, pd.DataFrame]:
    """ticker → M1 published outputs per session (index = date), each row from the
    walk-forward fold model that may serve that session (``model_asof``)."""
    plan = serving_plan(ml_root / "models", "m1", sessions, version)
    want = {t.upper() for t in tickers}
    frames = []
    for _path, (m, days) in sorted(plan.items()):
        if m.fold is None:  # the final fit serves holdout only, never scored here
            continue
        vdir = ml_root / "models" / "m1" / m.version
        df = pd.read_parquet(vdir / "oos_m1_features.parquet")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        keep = set(days)
        df = df[df["date"].isin(keep) & df["ticker"].astype(str).str.upper().isin(want)]
        frames.append(df)
    if not frames:
        return {}
    allr = pd.concat(frames, ignore_index=True)
    allr["ticker"] = allr["ticker"].astype(str).str.upper()
    return {
        str(t): g.drop(columns=["ticker"]).set_index("date").sort_index()
        for t, g in allr.groupby("ticker", sort=True)
    }


def m1_direction_of(outputs: Mapping[str, pd.DataFrame]) -> dict[str, pd.Series]:
    """ticker → session → ``m1_direction`` (+1 / −1 / 0; the stance proxy's input)."""
    return {t: df["m1_direction"].astype(float) for t, df in outputs.items()}


def m1_rows_of(df: pd.DataFrame | None) -> dict[dt.date, dict[str, float]] | None:
    """session → the M1 output columns (``m1_*``) of one ticker, for the path source."""
    if df is None or not len(df):
        return None
    cols = [c for c in df.columns if str(c).startswith("m1_")]
    return {
        d: {c: float(v) for c, v in zip(cols, row, strict=True)}
        for d, row in zip(df.index, df[cols].to_numpy(dtype=float), strict=True)
    }
