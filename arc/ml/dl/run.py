"""E27.6 end-to-end dev run: every challenger × family × fold, the stacker, the seed
variance, the replays and the ``dl_promote`` decision (D126; dev only, D84).

Output (gitignored, under ``<data>/ml/dl/<run>/``; ``<run>`` = ``dl-`` + a hash of the
config digest, the M2 versions and :data:`arc.ml.dl.config.PROTOCOL_REV`):

* ``<family>/<challenger>/<fold>/``: ``state_<seed>.pt``, ``inputs.json``,
  ``calibrators.json``, ``meta.json`` (epochs, τ, val IC, device, seconds),
  ``test_scores.parquet`` (ensemble + per-seed), ``replay_scores.parquet``;
* ``eval_metrics.csv`` / ``eval_seeds.csv`` / ``eval_replay.csv`` / ``eval_rule.csv`` /
  ``eval_cost.csv`` / ``eval_m1_surface.csv`` and ``manifest.json``.

Resumable: a ``<family>/<challenger>/<fold>/meta.json`` that exists is not refitted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from arc.ml import validate as v
from arc.ml.dl import pipeline as pl
from arc.ml.dl.config import CHALLENGERS, PROTOCOL_REV, RULE_KIND, STACK, DLConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from arc.ml.m2.train import Frame

SEQ_START = dt.date(2019, 6, 1)
DEV_END = dt.date(2023, 12, 31)


def run_id(dl: DLConfig) -> str:
    body = f"{dl.digest()}|{sorted(dl.m2_versions.items())}|{PROTOCOL_REV}"
    return "dl-" + hashlib.sha256(body.encode()).hexdigest()[:12]


def _ser(o: Any) -> Any:
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return str(o)


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, sort_keys=True, default=_ser) + "\n")


# ---------------------------------------------------------------------------
# Aux sources
# ---------------------------------------------------------------------------


def aux_source(
    kind: str,
    data_dir: Path,
    tickers: Sequence[str],
    dl: DLConfig,
    *,
    cache: dict[str, Any],
    workers: int,
    say: Callable[[str], None],
) -> pl.AuxSource:
    from arc.ml.dl import sequence as sq
    from arc.ml.dl import surface as sf

    if kind == "sequence":
        if "seq" not in cache:
            t0 = time.monotonic()
            cache["seq"] = sq.load_tables(data_dir, tickers, SEQ_START, DEV_END)
            say(f"  sequences: {len(cache['seq'].values)} vectors ({time.monotonic() - t0:.0f}s)")
        return pl.AuxSource("sequence", seq=cache["seq"], window=dl.seq_window)
    if kind == "surface":
        if "surf" not in cache:
            t0 = time.monotonic()
            sf.build_cache(
                data_dir, tickers, dt.date(2020, 1, 1), DEV_END, workers=workers, say=say
            )
            cache["surf"] = sf.load_cache(data_dir, tickers)
            say(f"  surfaces: {len(cache['surf'])} ({time.monotonic() - t0:.0f}s)")
        return pl.AuxSource("surface", surf=cache["surf"])
    return pl.AuxSource("none")


# ---------------------------------------------------------------------------
# Per family
# ---------------------------------------------------------------------------


def m2_test_scores(models_root: Path, family: str, version: str, fr: Frame, fold: v.Fold):
    """M2's out-of-sample scores of *fold*'s test rows: the registered version's fold
    model (``folds/<fold>``, trained before that year) scoring them through
    :func:`arc.ml.m2.predict.score_rows` (what ``oos_scores.parquet`` stores)."""
    from arc.ml.m2.predict import load_dir

    model = load_dir(models_root / family / version / "folds" / fold.name)
    return model.score(fr.df[fold.test_mask(fr.df)])


def _save_fold(
    fdir: Path, ff: pl.FoldFit, ens: pd.DataFrame, seeds: list[pd.DataFrame], rep: pd.DataFrame
) -> None:
    from arc.ml.dl.nets import load_torch

    torch = load_torch()
    fdir.mkdir(parents=True, exist_ok=True)
    for k, s in enumerate(ff.states):
        torch.save(s, fdir / f"state_{k}.pt")
    _write_json(fdir / "inputs.json", ff.inputs.model_dump(mode="json"))
    _write_json(
        fdir / "calibrators.json",
        {t: c.model_dump(mode="json") for t, c in sorted(ff.calib.items())},
    )
    parts = [ens.add_prefix("ens__")]
    parts += [s[["p_win", "p_stop", "e_r"]].add_prefix(f"s{k}__") for k, s in enumerate(seeds)]
    pd.concat(parts, axis=1).reset_index(names="row").to_parquet(fdir / "test_scores.parquet")
    rep.to_parquet(fdir / "replay_scores.parquet", index=False)
    _write_json(
        fdir / "meta.json",
        {
            "name": ff.name,
            "fold": ff.fold,
            "spec": ff.spec.model_dump(mode="json"),
            "epochs": ff.epochs,
            "tau_stop": ff.tau_stop,
            "val_ic": ff.val_ic,
            "aux": {k: x for k, x in ff.aux_meta.items() if k != "scaler"},
            "device": ff.device,
            "seconds": round(ff.seconds, 1),
            "cards": ff.cards,
            "val_curves": ff.val_curves,
        },
    )


def _load_fold(fdir: Path, n_seeds: int) -> tuple[pd.DataFrame, list[pd.DataFrame], dict]:
    t = pd.read_parquet(fdir / "test_scores.parquet").set_index("row")
    ens = t[[c for c in t.columns if c.startswith("ens__")]].rename(
        columns=lambda c: c.removeprefix("ens__")
    )
    seeds = [
        t[[f"s{k}__p_win", f"s{k}__p_stop", f"s{k}__e_r"]].rename(
            columns=lambda c, k=k: c.removeprefix(f"s{k}__")
        )
        for k in range(n_seeds)
    ]
    return ens, seeds, json.loads((fdir / "meta.json").read_text())


def _metric_rows(
    model: str, fold: str, fr: Frame, test: np.ndarray, s: pd.DataFrame
) -> pd.DataFrame:
    from arc.ml.m2.train import eval_rows

    d = fr.df[test]
    base = {
        "p_win:managed_pop": np.clip(
            pd.to_numeric(d["managed_pop"], errors="coerce").fillna(0.5).to_numpy(), 1e-4, 1 - 1e-4
        ),
    }
    return eval_rows(fold, d, s.loc[d.index], fr.labels, fr.weights[test], base).assign(model=model)


def _seed_rows(
    model: str, fold: str, fr: Frame, test: np.ndarray, seeds: list[pd.DataFrame]
) -> list[dict[str, Any]]:
    d = fr.df[test]
    w = fr.weights[test]
    yw = d[fr.labels["win"]].astype(float).to_numpy()
    yr = d[fr.labels["r"]].astype(float).to_numpy()
    out = []
    for k, s in enumerate(seeds):
        p = s.loc[d.index, "p_win"].to_numpy()
        ok = np.isfinite(yw)
        ic = v.daily_ic(d["date"].tolist(), s.loc[d.index, "e_r"].to_numpy(), yr)
        out.append(
            {
                "model": model,
                "fold": fold,
                "seed": k,
                "win_log_loss": v.log_loss(yw[ok], p[ok], w[ok]),
                "er_ic_mean": float(ic.mean()) if len(ic) else float("nan"),
            }
        )
    return out


def run_family(
    family: str,
    dl: DLConfig,
    *,
    data_dir: Path,
    models_root: Path,
    out: Path,
    challengers: Sequence[str],
    tickers: Sequence[str] | None,
    workers: int,
    say: Callable[[str], None],
) -> dict[str, Any]:
    """Fit + score every challenger on both folds; returns metrics, seeds, score maps."""
    from arc.ml.m2.predict import load_dir
    from arc.ml.m2.replay import score_map

    version = dl.m2_versions[family]
    t0 = time.monotonic()
    fr, cfg, _man = pl.family_frame(data_dir, family, version, models_root, tickers=tickers)
    say(f"{family}: {len(fr.df)} rows, {len(fr.features)} features ({time.monotonic() - t0:.0f}s)")
    cache: dict[str, Any] = {}
    names = sorted(set(fr.df["ticker"].astype(str)))
    metrics, seeds_rows, cost = [], [], []
    maps: dict[tuple[str, int], tuple[dict, float]] = {}
    oos_dl: dict[str, list[pd.DataFrame]] = {}
    oos_m2: list[pd.DataFrame] = []
    for fold in v.walk_forward():
        year = fold.test_start.year
        test = fold.test_mask(fr.df)
        m2s = m2_test_scores(models_root, family, version, fr, fold)
        metrics.append(_metric_rows("m2", fold.name, fr, test, m2s))
        rrows = pl.replay_rows(data_dir, family, year, cfg, tickers)
        say(f"  {family} {fold.name}: {int(test.sum())} test rows, {len(rrows)} replay rows")
        m2_fold = load_dir(models_root / family / version / "folds" / fold.name)
        m2_rep = m2_fold.score(rrows)
        taus = json.loads((models_root / family / version / "fold_taus.json").read_text())
        maps[("m2", year)] = (score_map(rrows, m2_rep), float(taus[fold.name]))
        ens_by: dict[str, pd.DataFrame] = {}
        rep_by: dict[str, pd.DataFrame] = {}
        val_ic: dict[str, float] = {}
        for name in challengers:
            spec = dl.nets[name]
            fdir = out / family / name / fold.name
            aux = aux_source(spec.aux, data_dir, names, dl, cache=cache, workers=workers, say=say)
            before = dt.date(year - 1, 1, 1)
            if (fdir / "meta.json").is_file():
                ens, seeds, meta = _load_fold(fdir, spec.seeds)
                rep = pd.read_parquet(fdir / "replay_scores.parquet")
                say(f"  {family} {name} {fold.name}: cached")
            else:
                say(f"  {family} {name} {fold.name}: fitting ({spec.seeds} seeds)")
                ff, ens, seeds = pl.fit_fold(name, spec, dl, cfg, fr, fold, aux, say=say)
                tr = time.monotonic()
                rep_s, _ = pl.predict_rows(ff, rrows, aux, before=before)
                rep = pd.concat(
                    [rrows[list(pl.REPLAY_COLS)], rep_s[["e_r", "p_stop", "p_win"]]], axis=1
                )
                _save_fold(fdir, ff, ens, seeds, rep)
                meta = json.loads((fdir / "meta.json").read_text())
                meta["replay_score_s"] = round(time.monotonic() - tr, 1)
                _write_json(fdir / "meta.json", meta)
            ens_by[name], rep_by[name], val_ic[name] = ens, rep, float(meta["val_ic"])
            metrics.append(_metric_rows(name, fold.name, fr, test, ens))
            seeds_rows += _seed_rows(name, fold.name, fr, test, seeds)
            maps[(name, year)] = (
                {
                    (d, str(t), str(k)): (float(e), float(p))
                    for d, t, k, e, p in zip(
                        pd.to_datetime(rep["date"]).dt.date,
                        rep["ticker"],
                        rep["key"],
                        rep["e_r"],
                        rep["p_stop"],
                        strict=True,
                    )
                },
                float(meta["tau_stop"]),
            )
            cost.append(
                {
                    "family": family,
                    "model": name,
                    "fold": fold.name,
                    "seconds": meta["seconds"],
                    "device": meta["device"],
                    "epochs": ",".join(str(e) for e in meta["epochs"]),
                    "val_ic": meta["val_ic"],
                    "aux_coverage": meta.get("aux", {}).get("coverage"),
                }
            )
        if len(ens_by) and STACK not in challengers:
            best = max(ens_by, key=lambda n: (np.nan_to_num(val_ic[n], nan=-9), n))
            for name, e in ens_by.items():
                oos_dl.setdefault(name, []).append(e)
            oos_m2.append(m2s)
            # every OOS row so far (earlier test years + this one); the stacker reads
            # only the ones whose label closed before each block
            m2_all = pd.concat(oos_m2)
            dl_all = pd.concat(oos_dl[best]).loc[m2_all.index]
            d = fr.df.loc[m2_all.index]
            ymat = pl.target_matrix(fr)
            pos = fr.df.index.get_indexer(m2_all.index)
            st = pl.fit_stacker(
                np.asarray(d["date"].tolist()),
                np.asarray(d["label_end_date"].tolist()),
                fr.sessions,
                m2_all,
                dl_all,
                ymat[pos],
                fr.weights[pos],
                year,
            )
            dt_ = fr.df[test]
            dates = np.asarray(dt_["date"].tolist())
            stk = pl.stacked_outputs(m2s.loc[dt_.index], ens_by[best].loc[dt_.index], st, dates)
            metrics.append(_metric_rows(STACK, fold.name, fr, test, stk).assign(best_dl=best))
            # replay scores: blend M2's replay scores with the best challenger's
            rb = rep_by[best].reset_index(drop=True)
            m2r = m2_rep.reset_index(drop=True)
            rdates = np.asarray(pd.to_datetime(rrows["date"]).dt.date.tolist())
            sr = st.apply(rdates, m2r, rb)
            # τ for the stack, ex ante: the M2 percentile of the stacked p_stop over the
            # earlier OOS years' rows; M2's own fold τ when there are none (first fold)
            prior = np.asarray(d["date"].tolist()) < fold.test_start
            tau = float(taus[fold.name])
            if prior.sum() >= pl.STACK_MIN_ROWS:
                pd_ = np.asarray(d["date"].tolist())[prior]
                ps = st.apply(pd_, m2_all[prior], dl_all[prior])["p_stop"].dropna()
                tau = float(np.percentile(ps, cfg.p_stop_pct))
            maps[(STACK, year)] = (
                {
                    (d0, str(t), str(k)): (float(e), float(p))
                    for d0, t, k, e, p in zip(
                        rdates, rrows["ticker"], rrows["key"], sr["e_r"], sr["p_stop"], strict=True
                    )
                },
                tau,
            )
            _write_json(
                out / family / STACK / fold.name / "meta.json",
                {
                    "best_dl": best,
                    "val_ic": val_ic,
                    "coefs": st.coefs,
                    "n_fit": st.n_fit,
                    "tau_stop": tau,
                },
            )
        del rrows, m2_rep
    return {
        "metrics": pd.concat(metrics, ignore_index=True).assign(family=family),
        "seeds": pd.DataFrame(seeds_rows).assign(family=family),
        "cost": pd.DataFrame(cost),
        "maps": maps,
        "cfg": cfg,
        "rows": len(fr.df),
    }


# ---------------------------------------------------------------------------
# Replays and rule
# ---------------------------------------------------------------------------


def replay(
    data_dir: Path,
    maps: dict[str, dict[tuple[str, int], tuple[dict, float]]],
    *,
    workers: int,
    tickers: Sequence[str] | None,
    say: Callable[[str], None],
) -> pd.DataFrame:
    """One sim build per test year; per family: incumbent, then ``ml_er`` and
    ``ml_pstop`` for M2 and every challenger (E27.3's replay walk and summary)."""
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.sim_report import build_sim, walk
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config
    from arc.ml.m2.config import FAMILY_PROFILE
    from arc.ml.m2.replay import Override, summarize

    cfg = load_ranking_file()
    eq0 = float(cfg.backtest.sim.equity)
    rows = []
    for year in v.TEST_YEARS:
        stop = dt.date(year, 12, 31)
        b = build_sim(
            data_dir=data_dir,
            universe="c500",
            start=dt.date(year, 1, 1),
            end=stop,
            data_end=stop + dt.timedelta(days=95),
            profiles=[FAMILY_PROFILE[f] for f in maps],
            rankers=[],
            cfg=cfg,
            cost=load_cost_model(),
            exits=load_exit_config(),
            settings=ArcSettings(),
            tickers=tickers,
            workers=workers,
        )
        say(f"  sim build {year}: {len(b.names)} names, {b.build_s:.0f}s")
        for fam, fmaps in maps.items():
            prof = FAMILY_PROFILE[fam]
            inc = b.rankers[prof][0]
            run = walk(b, profile=prof, ranker=inc, cfg=cfg, end=stop)
            rows.append(
                {
                    "family": fam,
                    "model": "incumbent",
                    **summarize(run, eq0, use="incumbent", year=year, cov=None),
                }
            )
            for (model, y), (sm, tau) in sorted(fmaps.items()):
                if y != year:
                    continue
                for use in ("ml_er", "ml_pstop"):
                    ov = Override(use, sm, tau)
                    run = walk(b, profile=prof, ranker=inc, cfg=cfg, end=stop, key_override=ov)
                    cov = ov.hit / max(ov.hit + ov.miss, 1)
                    rows.append(
                        {
                            "family": fam,
                            "model": model,
                            **summarize(run, eq0, use=use, year=year, cov=cov),
                        }
                    )
            say(f"  replay {fam} {year}: done")
        del b
    return pd.DataFrame(rows)


def rule_table(metrics: pd.DataFrame, rep: pd.DataFrame | None) -> pd.DataFrame:
    """``dl_promote`` per family × challenger × fold (see :mod:`arc.ml.dl.config`), plus
    the absolute ``m2_promote`` legs for the record."""
    m = metrics[(metrics.split == "All") & (metrics.group == "all")]
    out = []
    for (fam, fold), g in m.groupby(["family", "fold"]):
        ref = g[g.model == "m2"].iloc[0]
        year = int(str(fold).removeprefix("test"))
        for _, r in g[g.model != "m2"].iterrows():
            row: dict[str, Any] = {
                "family": fam,
                "model": r.model,
                "fold": fold,
                "ll": r.win_log_loss,
                "ll_m2": ref.win_log_loss,
                "ll_managed_pop": r.get("win_log_loss_managed_pop"),
                "er_ic": r.er_ic_mean,
                "er_ic_t": r.er_ic_nw_t,
                "er_ic_m2": ref.er_ic_mean,
                "beats_m2_ll": bool(r.win_log_loss < ref.win_log_loss),
                "beats_m2_ic": bool(r.er_ic_mean > ref.er_ic_mean and r.er_ic_nw_t > 2),  # noqa: PLR2004
            }
            dv = False
            if rep is not None and len(rep):
                q = rep[(rep.family == fam) & (rep.year == year) & (rep.use == "ml_er")]
                me, mm = q[q.model == r.model], q[q.model == "m2"]
                inc = rep[(rep.family == fam) & (rep.year == year) & (rep.model == "incumbent")]
                if len(me) and len(mm):
                    me0, mm0 = me.iloc[0], mm.iloc[0]
                    row |= {
                        "net": me0.net_pnl,
                        "net_m2": mm0.net_pnl,
                        "dd": me0.max_dd_pct,
                        "dd_m2": mm0.max_dd_pct,
                    }
                    dv = bool(me0.net_pnl > mm0.net_pnl and me0.max_dd_pct > mm0.max_dd_pct)
                    if len(inc):
                        i0 = inc.iloc[0]
                        row["net_incumbent"] = i0.net_pnl
                        row["beats_incumbent"] = bool(
                            me0.net_pnl > i0.net_pnl and me0.max_dd_pct > i0.max_dd_pct
                        )
            row["beats_m2_value"] = dv
            row["year_pass"] = bool(row["beats_m2_ll"] and row["beats_m2_ic"] and dv)
            out.append(row)
    return pd.DataFrame(out)


def decisions(rule: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (fam, model), g in rule.groupby(["family", "model"]):
        passed = bool(len(g) >= 2 and g["year_pass"].all())  # noqa: PLR2004
        rows.append(
            {
                "family": fam,
                "model": model,
                "years_passed": int(g["year_pass"].sum()),
                "dl_promote": passed,
                "decision": "joins as challenger (shadow review)" if passed else "negative result",
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def dev_run(
    families: Sequence[str],
    dl: DLConfig,
    *,
    data_dir: Path,
    out_root: Path | None = None,
    code_sha: str,
    now: dt.datetime,
    challengers: Sequence[str] = CHALLENGERS,
    tickers: Sequence[str] | None = None,
    workers: int = 6,
    do_replay: bool = True,
    m1_surface: bool = True,
    say: Callable[[str], None] = lambda _m: None,
) -> Path:
    from arc.ml.features.registry import schema_version

    models_root = data_dir / "ml" / "models"
    out = (out_root or data_dir / "ml" / "dl") / run_id(dl)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    res = {}
    for fam in families:
        res[fam] = run_family(
            fam,
            dl,
            data_dir=data_dir,
            models_root=models_root,
            out=out,
            challengers=challengers,
            tickers=tickers,
            workers=workers,
            say=say,
        )
    metrics = pd.concat([r["metrics"] for r in res.values()], ignore_index=True)
    seeds = pd.concat([r["seeds"] for r in res.values()], ignore_index=True)
    cost = pd.concat([r["cost"] for r in res.values()], ignore_index=True)
    metrics.to_csv(out / "eval_metrics.csv", index=False)
    seeds.to_csv(out / "eval_seeds.csv", index=False)
    cost.to_csv(out / "eval_cost.csv", index=False)
    rep = None
    if do_replay:
        tr = time.monotonic()
        rep = replay(
            data_dir,
            {f: r["maps"] for f, r in res.items()},
            workers=workers,
            tickers=tickers,
            say=say,
        )
        rep.to_csv(out / "eval_replay.csv", index=False)
        say(f"replays {time.monotonic() - tr:.0f}s")
    rule = rule_table(metrics, rep)
    rule.to_csv(out / "eval_rule.csv", index=False)
    dec = decisions(rule)
    dec.to_csv(out / "eval_decisions.csv", index=False)
    if m1_surface:
        from arc.ml.dl.m1_surface import run as m1_run

        m1 = m1_run(data_dir, dl, tickers=tickers, workers=workers, say=say)
        m1.to_csv(out / "eval_m1_surface.csv", index=False)
    _write_json(
        out / "manifest.json",
        {
            "run": out.name,
            "rule": RULE_KIND,
            "protocol_rev": PROTOCOL_REV,
            "config": dl.model_dump(mode="json"),
            "config_digest": dl.digest(),
            "m2_versions": dl.m2_versions,
            "feature_schema": schema_version(),
            "code_sha": code_sha,
            "created": now.isoformat(),
            "families": list(families),
            "challengers": list(challengers),
            "rows": {f: r["rows"] for f, r in res.items()},
            "tickers": "c500 (every ledger ticker)" if tickers is None else list(tickers),
            "replayed": rep is not None,
            "holdout_reads": 0,
            "total_s": round(time.monotonic() - t0, 1),
        },
    )
    return out
