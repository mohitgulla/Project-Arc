"""M2 end-to-end runs (E27.3): dev evaluation + replays + rule + registry, and the
single holdout evaluation.

``dev_run`` per family: :func:`arc.ml.m2.train.train_family` (walk-forward, final fit),
then the decision-value replays of each test year with that fold's out-of-sample
scores, then the ``m2_promote`` rule, then registration (``candidate``, or
``shadow`` when the rule passed). ``holdout_run`` scores 2024 → the ledger end once
with the final model, after the registry has logged (and authorised) the read.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from arc.ml import registry as reg
from arc.ml import validate as v
from arc.ml.m2 import dataset as ds
from arc.ml.m2 import replay as rp
from arc.ml.m2.config import FAMILY_PROFILE, RULE_KIND, M2Config

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from arc.ml.m2.predict import M2Model


def finalize_manifest(vdir: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Re-hash every file of the version (except the manifest) into ``files``."""
    man = json.loads((vdir / "manifest.json").read_text())
    names = sorted(
        str(p.relative_to(vdir))
        for p in vdir.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    )
    man["files"] = reg.hash_files(vdir, names)
    if extra:
        man.update(extra)
    (vdir / "manifest.json").write_text(
        json.dumps(man, indent=2, sort_keys=True, default=str) + "\n"
    )
    return man


def scored_rows(
    model: M2Model,
    ledger_root: Path,
    features_root: Path,
    family: str,
    years: Sequence[int],
    *,
    cfg: M2Config,
    split: str = "dev",
    reason: str | None = None,
    passes_filters: bool = True,
    cap_per_day: int | None = None,
    end: dt.date | None = None,
    tickers: Sequence[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Ledger rows (full, or capped) of *years* with their M2 scores."""
    from arc.ml.features.registry import schema_version
    from arc.ml.m2.train import panel_columns

    rows = ds.load_family(
        ledger_root,
        FAMILY_PROFILE[family],
        years,
        cfg.label_x,
        cap_per_day=cap_per_day,
        split=split,
        holdout_reason=reason,
        filter_rows="passes_filters" if passes_filters else None,
        for_scoring=passes_filters,
        tickers=tickers,
    )
    if end is not None:
        rows = rows[rows["date"] <= end].reset_index(drop=True)
    pcols = panel_columns()
    panel = ds.load_panel_range(
        features_root,
        dt.date(min(years), 1, 1),
        dt.date(max(years), 12, 31),
        schema_version(),
        pcols,
    )
    df = ds.attach_features(rows, panel, pcols)
    parts = []
    step = 400_000
    for i in range(0, len(df), step):
        parts.append(model.score(df.iloc[i : i + step]))
    return df, pd.concat(parts)


def dev_replays(
    trained: dict[str, tuple[Path, M2Config]],
    *,
    data_dir: Path,
    ledger_root: Path,
    features_root: Path,
    tickers: Sequence[str] | None,
    workers: int,
    say: Callable[[str], None],
) -> pd.DataFrame:
    """One sim build per test year, every family's uses walked on it."""
    from arc.ml.m2.predict import load_dir

    out = []
    for fold in v.walk_forward():
        year = fold.test_start.year
        fams: dict[str, tuple[str, rp.ScoreMap, float]] = {}
        for family, (vdir, cfg) in trained.items():
            taus = json.loads((vdir / "fold_taus.json").read_text())
            model = load_dir(vdir / "folds" / fold.name)
            rows, s = scored_rows(
                model, ledger_root, features_root, family, [year], cfg=cfg, tickers=tickers
            )
            sm = rp.score_map(rows, s)
            del rows, s
            say(f"  {family} {year}: {len(sm)} scored candidates, tau {taus[fold.name]:.3f}")
            fams[family] = (FAMILY_PROFILE[family], sm, float(taus[fold.name]))
        out.append(
            rp.replay_year(
                data_dir=data_dir,
                year=year,
                families=fams,
                workers=workers,
                tickers=tickers,
                say=say,
            )
        )
    return pd.concat(out, ignore_index=True)


def dev_run(
    families: Sequence[str],
    cfg: M2Config,
    *,
    data_dir: Path,
    out_root: Path,
    code_sha: str,
    now: dt.datetime,
    tickers: Sequence[str] | None = None,
    workers: int = 6,
    replay: bool = True,
    register: bool = True,
    say: Callable[[str], None] = lambda _m: None,
) -> dict[str, dict[str, Any]]:
    from arc.ml.m2.train import train_family

    ledger_root = data_dir / "ml" / "ledger"
    features_root = data_dir / "ml" / "features"
    runs = {}
    for family in families:
        runs[family] = train_family(
            family,
            cfg,
            ledger_root=ledger_root,
            features_root=features_root,
            out_root=out_root,
            code_sha=code_sha,
            now=now,
            tickers=tickers,
            progress=say,
        )
    rep = None
    if replay:
        rep = dev_replays(
            {f: (r.vdir, cfg) for f, r in runs.items()},
            data_dir=data_dir,
            ledger_root=ledger_root,
            features_root=features_root,
            tickers=tickers,
            workers=workers,
            say=say,
        )
    out: dict[str, dict[str, Any]] = {}
    for family, run in runs.items():
        vdir = run.vdir
        rule = run.checks.assign(replay_pass=False, year_pass=False)
        if rep is not None:
            fr = rep[rep.family == family]
            fr.to_csv(vdir / "eval_replay.csv", index=False)
            rule = rp.apply_rule(run.checks, rp.replay_checks(fr))
        rule.to_csv(vdir / "eval_rule.csv", index=False)
        passed = rp.rule_passed(rule) if rep is not None else False
        man = finalize_manifest(vdir, {"rule_passed": passed, "replayed": rep is not None})
        if register:
            all_row = run.metrics[(run.metrics.split == "All") & (run.metrics.group == "all")]
            reg.register(
                out_root,
                family,
                run.version,
                now=now,
                rule=RULE_KIND,
                rule_passed=passed,
                feature_schema=man["feature_schema"],
                summary={
                    "win_log_loss": dict(
                        zip(all_row["fold"], all_row["win_log_loss"].round(5), strict=True)
                    ),
                    "er_ic": dict(
                        zip(all_row["fold"], all_row["er_ic_mean"].round(4), strict=True)
                    ),
                    "rule_passed": passed,
                },
            )
        say(f"{family}: {run.version} rule {'PASSED' if passed else 'failed'}")
        out[family] = {
            "version": run.version,
            "vdir": vdir,
            "rule_passed": passed,
            "timings": run.timings,
        }
    return out


def holdout_run(
    family: str,
    version: str,
    *,
    data_dir: Path,
    out_root: Path,
    code_sha: str,
    now: dt.datetime,
    reason: str,
    actor: str | None = None,
    owner: str | None = None,
    ack: str | None = None,
    workers: int = 6,
    replay: bool = True,
    say: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """Score the holdout once with the registered final model (metrics + replay).

    The registry logs the read first and refuses a second one without the owner's
    ``OWNER UPDATE: holdout re-read ok`` (:func:`arc.ml.registry.authorize_holdout_read`).
    """
    from arc.ml.m2.predict import load_model
    from arc.ml.m2.train import eval_rows, reliability_rows

    model = load_model(out_root, family, version)
    n = reg.authorize_holdout_read(
        out_root,
        family,
        version,
        now=now,
        code_sha=code_sha,
        reason=reason,
        actor=actor,
        owner=owner,
        ack=ack,
    )
    cfg = M2Config.model_validate(model.manifest["config"])
    ledger_root = data_dir / "ml" / "ledger"
    features_root = data_dir / "ml" / "features"
    years = [2024, 2025, 2026]
    rows, s = scored_rows(
        model,
        ledger_root,
        features_root,
        family,
        years,
        cfg=cfg,
        split="holdout",
        reason=reason,
        passes_filters=False,
        cap_per_day=cfg.cap_per_day,
    )
    labels = ds.label_cols(cfg.label_x)
    w = v.sample_weights(rows, rows["inv_keep"].to_numpy(dtype=float))
    rows = rows.reset_index(drop=True)
    s = s.reset_index(drop=True)
    base = {
        "p_win:managed_pop": np.clip(
            pd.to_numeric(rows["managed_pop"], errors="coerce").fillna(0.5).to_numpy(),
            1e-4,
            1 - 1e-4,
        ),
        "p_win:static_pop": np.clip(
            pd.to_numeric(rows["static_pop"], errors="coerce").fillna(0.5).to_numpy(),
            1e-4,
            1 - 1e-4,
        ),
    }
    met = eval_rows("holdout", rows, s, labels, w, base)
    rel = reliability_rows("holdout", rows, s, labels, w)
    vdir = out_root / family / version
    met.to_csv(vdir / "holdout_metrics.csv", index=False)
    rel.to_csv(vdir / "holdout_reliability.csv", index=False)
    rep = None
    if replay:
        hrows, hs = scored_rows(
            model,
            ledger_root,
            features_root,
            family,
            years,
            cfg=cfg,
            split="holdout",
            reason=reason,
        )
        sm = rp.score_map(hrows, hs)
        del hrows, hs
        end = max(k[0] for k in sm)
        rep = rp.replay_year(
            data_dir=data_dir,
            year=2024,
            families={family: (FAMILY_PROFILE[family], sm, model.tau_stop)},
            workers=workers,
            end=end,
            say=say,
        )
        rep["year"] = f"2024-{end.isoformat()}"
        rep.to_csv(vdir / "holdout_replay.csv", index=False)
    finalize_manifest(
        vdir,
        {"holdout_reads": n, "holdout_read_at": now.isoformat(), "holdout_read_sha": code_sha},
    )
    return {"reads": n, "metrics": met, "replay": rep}
