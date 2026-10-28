"""E27.4 (D140) monthly ``ml.retrain``: rolling window, gate, candidate-only registry,
rolling reads, the promote path and the routine wiring."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

from arc.ml import registry as reg
from arc.ml import retrain as rt
from arc.routines.config import Days
from arc.routines.schedule import day_matches
from tests.ml_lightgbm import require_lightgbm

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 11, 7, 10, 0)
OWNER = "U_OWNER"


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def test_window_uses_completed_months_gap_and_expanding_train() -> None:
    w = rt.plan_window(dt.date(2026, 10, 15), 60)
    # resolved through Aug 16 -> Aug is incomplete, July is the last completed month
    assert (w.holdout_start, w.holdout_end) == (dt.date(2026, 2, 1), dt.date(2026, 7, 31))
    assert w.train_end == dt.date(2026, 2, 1) - dt.timedelta(days=61)
    assert w.train_start == dt.date(2020, 1, 1)
    assert (w.holdout_start - w.train_end).days > w.gap_days


def test_window_month_end_exactly_resolved_counts_and_slides_monthly() -> None:
    w = rt.plan_window(dt.date(2026, 9, 29), 60)  # resolved = Jul 31 (a month end)
    assert w.holdout_end == dt.date(2026, 7, 31)
    nxt = rt.plan_window(dt.date(2026, 10, 30), 60)
    assert nxt.holdout_start == dt.date(2026, 3, 1)
    assert nxt.train_end > w.train_end  # the train window gains a month per run


def test_window_capped_by_last_decision_date() -> None:
    w = rt.plan_window(dt.date(2026, 10, 15), 45, last_decision=dt.date(2026, 6, 10))
    assert w.holdout_end == dt.date(2026, 5, 31)


@pytest.mark.parametrize(("gap", "months"), [(0, 6), (45, 0)])
def test_window_rejects_bad_inputs(gap: int, months: int) -> None:
    with pytest.raises(ValueError, match="must be"):
        rt.plan_window(dt.date(2026, 10, 15), gap, months=months)


def test_window_without_training_data_is_refused() -> None:
    with pytest.raises(ValueError, match="no training data"):
        rt.plan_window(dt.date(2020, 9, 1), 45)


def test_rolling_folds_are_12_month_blocks_ending_at_train_end() -> None:
    w = rt.plan_window(dt.date(2026, 10, 15), 60)
    folds = rt.rolling_folds(w)
    assert [f.test_end for f in folds][-1] == w.train_end
    assert len(folds) == rt.N_FOLDS
    assert folds[0].test_end < folds[1].test_start
    assert [p.name for p in rt.m1_folds(w)] == [f.name for f in folds]


def test_gap_days_is_the_longest_label_span_floored(tmp_path: Path) -> None:
    v = tmp_path / "v1"
    p = v / "profile=margin" / "year=2026"
    p.mkdir(parents=True)
    (v / "manifest.json").write_text("{}")
    pd.DataFrame(
        {
            "date": [dt.date(2026, 1, 2), dt.date(2026, 1, 5)],
            "label_end_date": [dt.date(2026, 3, 4), dt.date(2026, 1, 9)],
        }
    ).to_parquet(p / "part-AAA.parquet")
    assert rt.gap_days(v, years=[2026]) == 61  # noqa: PLR2004
    assert rt.gap_days(v, years=[2026], floor=90) == 90  # noqa: PLR2004
    assert rt.gap_days(v, years=[2025]) == rt.MIN_GAP_DAYS


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _row(model: str, **kw: Any) -> dict[str, Any]:
    base = {
        "model": model,
        "split": "All",
        "group": "all",
        "value": "all",
        "n": 500,
        "status": "ok",
        "win_log_loss": 0.60,
        "win_log_loss_managed_pop": 0.65,
        "er_ic_mean": 0.05,
        "er_ic_t": 3.0,
    }
    return {**base, **kw}


def test_gate_passes_a_better_candidate_and_names_its_legs() -> None:
    g = rt.gate([_row("candidate"), _row("incumbent", win_log_loss=0.62)], incumbent=True)
    assert g["passed"] and g["status"] == "pass"
    assert set(g["legs"]) == {
        "beats_managed_pop",
        "ic_pass",
        "ll_not_worse_than_incumbent",
        "ic_not_worse_than_incumbent",
    }


@pytest.mark.parametrize(
    "cand",
    [
        {"win_log_loss": 0.70},  # loses to managed PoP
        {"er_ic_t": 1.0},  # IC not significant
        {"er_ic_mean": -0.01},
        {"win_log_loss": 0.64},  # worse than the incumbent
    ],
)
def test_gate_fails(cand: dict[str, Any]) -> None:
    g = rt.gate([_row("candidate", **cand), _row("incumbent", win_log_loss=0.62)], incumbent=True)
    assert not g["passed"] and g["status"] == "fail"


def test_insufficient_slices_never_decide() -> None:
    g = rt.gate([_row("candidate", n=12, status="insufficient")], incumbent=False)
    assert g == {"passed": False, "status": "insufficient", "detail": "holdout n=12 < 30"}
    assert rt.gate([], incumbent=False)["status"] == "insufficient"
    # an insufficient incumbent slice is ignored (the candidate is judged on its own)
    g = rt.gate(
        [_row("candidate"), _row("incumbent", n=5, status="insufficient", win_log_loss=0.1)],
        incumbent=True,
    )
    assert g["passed"] and "ll_not_worse_than_incumbent" not in g["legs"]


def test_holdout_table_reports_n_per_slice_and_marks_insufficient() -> None:
    rng = np.random.default_rng(1)
    n = 80
    rows = pd.DataFrame(
        {
            "date": [dt.date(2026, 3, 2) + dt.timedelta(days=i % 20) for i in range(n)],
            "kind": ["bull_put"] * 60 + ["iron_condor"] * 20,
            "sleeve": ["swing"] * n,
            "ctx_etf": [False] * 70 + [True] * 10,
            "label_win_x025": rng.uniform(size=n) < 0.6,  # noqa: PLR2004
            "label_r_net_x025": rng.normal(size=n),
            "managed_pop": rng.uniform(0.3, 0.8, size=n),
        }
    )
    s = pd.DataFrame({"p_win": rng.uniform(0.2, 0.8, size=n), "e_r": rng.normal(size=n)})
    table = rt.holdout_table(
        rows, {"candidate": s}, {"win": "label_win_x025", "r": "label_r_net_x025"}, np.ones(n)
    )
    by = {(r["split"], r["group"], r["value"]): r for r in table}
    assert by[("All", "all", "all")]["n"] == n
    assert by[("All", "kind", "bull_put")]["status"] == "ok"
    assert by[("All", "kind", "iron_condor")]["status"] == "insufficient"
    assert by[("ETFs", "all", "all")]["n"] == 10  # noqa: PLR2004
    assert by[("ETFs", "all", "all")]["status"] == "insufficient"
    assert "win_log_loss_managed_pop" in by[("All", "all", "all")]


# ---------------------------------------------------------------------------
# Registry: candidate only, rolling reads, promote
# ---------------------------------------------------------------------------


def _version(root: Path, family: str, version: str) -> None:
    vdir = root / family / version
    vdir.mkdir(parents=True)
    (vdir / "a.bin").write_bytes(b"x")
    (vdir / "manifest.json").write_text(
        json.dumps({"files": reg.hash_files(vdir, ["a.bin"]), "feature_schema": "s"})
    )


def test_retrain_registers_candidate_even_when_the_rule_passed(tmp_path: Path) -> None:
    _version(tmp_path, "credit", "m2-aaa")
    e = reg.register(
        tmp_path, "credit", "m2-aaa", now=NOW, rule="r", rule_passed=True, auto_shadow=False
    )
    assert e.status == "candidate"
    _version(tmp_path, "credit", "m2-bbb")
    e = reg.register(tmp_path, "credit", "m2-bbb", now=NOW, rule="r", rule_passed=True)
    assert e.status == "shadow"  # E27.3 path unchanged (D133)


def test_rolling_reads_are_kept_apart_from_the_fixed_holdout(tmp_path: Path) -> None:
    reg.log_rolling_read(tmp_path, "credit", {"version": "m2-aaa", "passed": True, "n": 40})
    assert reg.holdout_reads(tmp_path, "credit") == []
    (read,) = reg.rolling_reads(tmp_path, "credit")
    assert read["kind"] == reg.ROLLING and read["family"] == "credit"


def test_owner_promotes_a_candidate_only_after_a_passed_rolling_read(tmp_path: Path) -> None:
    _version(tmp_path, "credit", "m2-aaa")
    reg.register(
        tmp_path, "credit", "m2-aaa", now=NOW, rule="r", rule_passed=True, auto_shadow=False
    )
    with pytest.raises(reg.RegistryError):  # no rolling read: a plain candidate
        reg.check_promotable(tmp_path, "credit", "m2-aaa", actor=OWNER, owner=OWNER)
    reg.log_rolling_read(tmp_path, "credit", {"version": "m2-aaa", "passed": False})
    with pytest.raises(reg.RegistryError, match="rolling-holdout gate failed"):
        reg.check_promotable(tmp_path, "credit", "m2-aaa", actor=OWNER, owner=OWNER)
    reg.log_rolling_read(tmp_path, "credit", {"version": "m2-aaa", "passed": True})
    with pytest.raises(reg.RegistryError, match="not the owner"):
        reg.check_promotable(tmp_path, "credit", "m2-aaa", actor="U_X", owner=OWNER)
    idx = reg.promote(tmp_path, "credit", "m2-aaa", actor=OWNER, owner=OWNER, now=NOW)
    assert idx.champion == "m2-aaa"
    log = (tmp_path / "credit" / reg.PROMOTIONS).read_text().splitlines()
    assert json.loads(log[-1])["old"] == "candidate"


# ---------------------------------------------------------------------------
# Ledger extension bookkeeping
# ---------------------------------------------------------------------------


def test_extension_years_and_marker_reset_are_resumable(tmp_path: Path) -> None:
    assert rt.extension_years(dt.date(2027, 2, 1)) == [2026, 2027]
    assert rt.extension_years(dt.date(2026, 10, 15)) == [2026]
    root = tmp_path / "ledger"
    v = root / "v1"
    for y, t in ((2025, "AAA"), (2026, "AAA"), (2026, "BBB")):
        (v / "_done" / str(y)).mkdir(parents=True, exist_ok=True)
        (v / "_done" / str(y) / t).touch()
    assert rt._ledger_tickers(v) == ["AAA", "BBB"]
    end = dt.date(2026, 10, 15)
    assert rt.reset_markers(root, [2026], end) == 2  # noqa: PLR2004
    assert not (v / "_done" / "2026").exists() and (v / "_done" / "2025" / "AAA").exists()
    assert rt.pinned_extension(root) == end
    (v / "_done" / "2026").mkdir()
    (v / "_done" / "2026" / "AAA").touch()  # resumed work keeps its markers
    assert rt.reset_markers(root, [2026], end) == 0
    assert (v / "_done" / "2026" / "AAA").exists()


# ---------------------------------------------------------------------------
# Schedule + routine wiring
# ---------------------------------------------------------------------------


def test_month_first_sat_schedule() -> None:
    sats = [d for d in (dt.date(2026, 11, 1) + dt.timedelta(days=i) for i in range(30))]
    hits = [d for d in sats if day_matches(Days.MONTH_FIRST_SAT, d)]
    assert hits == [dt.date(2026, 11, 7)]
    assert day_matches(Days.MONTH_FIRST_SAT, dt.date(2026, 8, 1))  # the 1st is a Saturday
    assert not day_matches(Days.MONTH_FIRST_SAT, dt.date(2026, 8, 8))


def test_routines_register_refresh_and_retrain() -> None:
    from arc.routines.config import DEFAULT_ROUTINES_PATH, Lane, load_routines
    from arc.routines.handlers import BUILTIN_HANDLERS

    c = load_routines(DEFAULT_ROUTINES_PATH)
    jobs = c.jobs()
    for job in ("ml.refresh", "ml.retrain"):
        assert job in BUILTIN_HANDLERS
        assert jobs[job][1].lane is Lane.BACKGROUND
    assert jobs["ml.retrain"][1].days is Days.MONTH_FIRST_SAT
    assert [str(t) for t in jobs["ml.refresh"][1].schedule] == ["20:45:00"]


class _Ctx:
    """The slice of JobContext the ML handlers read."""

    def __init__(self, options: dict[str, Any], now: dt.datetime) -> None:
        from types import SimpleNamespace

        self.spec = SimpleNamespace(options=options)
        self.now = now
        self.beats = 0

    def heartbeat(self) -> None:
        self.beats += 1

    @property
    def clock(self):  # noqa: ANN201
        return lambda: self.now


def _et(*a: int) -> dt.datetime:
    from arc.utils.calendar import ET

    return dt.datetime(*a, tzinfo=ET)


def test_retrain_handler_skips_until_the_ledger_extension_completes(tmp_path: Path) -> None:
    from arc.routines.handlers import JobSkippedError, ml_retrain_source

    (tmp_path / "ok").mkdir()
    posts: list[str] = []
    ctx = _Ctx({"stop_at": "21:00"}, _et(2026, 11, 7, 10, 0))

    def extend(_d: Path, **kw: Any) -> dict[str, Any]:
        assert kw["stop"]() is False and ctx.beats == 1
        return {"data_end": "2026-11-06", "pending": 120, "complete": False}

    def boom(**_kw: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError

    import arc.ml.refresh as refresh

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(refresh, "history_cache_end", lambda *_a, **_k: dt.date(2026, 11, 6))
        with pytest.raises(JobSkippedError, match="120 ticker"):
            ml_retrain_source(
                ctx,  # type: ignore[arg-type]
                data_dir=tmp_path,
                post=posts.append,
                extend=extend,
                retrain=boom,
            )
    assert "no retrain this time" in posts[0]


def test_retrain_handler_posts_one_line_and_reports(tmp_path: Path) -> None:
    from arc.routines.handlers import ml_retrain_source

    posts: list[str] = []
    ctx = _Ctx({"families": ["credit"], "holdout_months": 6}, _et(2026, 11, 7, 10, 0))
    seen: dict[str, Any] = {}

    def retrain(**kw: Any) -> rt.ChildRetrain:
        seen.update(kw)
        res = rt.RetrainResult(window=rt.plan_window(dt.date(2026, 11, 6), 60))
        res.families["credit"] = rt.FamilyResult(
            "credit", version="m2-new", gate={"status": "pass", "passed": True}, n_holdout=900
        )
        return rt.ChildRetrain(res.summary(), res.metrics())

    import arc.ml.refresh as refresh

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(refresh, "history_cache_end", lambda *_a, **_k: dt.date(2026, 11, 6))
        out = ml_retrain_source(
            ctx,  # type: ignore[arg-type]
            data_dir=tmp_path,
            post=posts.append,
            extend=lambda *_a, **_k: {"complete": True, "data_end": "2026-11-06"},
            retrain=retrain,
        )
    assert seen["families"] == ["credit"] and seen["months"] == 6  # noqa: PLR2004
    assert seen["timeout_s"] == 4 * 3600 and seen["code_sha"]
    assert len(posts) == 1 and "candidate only" in posts[0] and "m2-new pass" in posts[0]
    assert out.metrics["families"]["credit"]["gate"] == "pass"


def test_retrain_handler_skips_without_a_history_cache(tmp_path: Path) -> None:
    from arc.routines.handlers import JobSkippedError, ml_retrain_source

    ctx = _Ctx({}, _et(2026, 11, 7, 10, 0))
    import arc.ml.refresh as refresh

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(refresh, "history_cache_end", lambda *_a, **_k: None)
        with pytest.raises(JobSkippedError, match="no history cache"):
            ml_retrain_source(ctx, data_dir=tmp_path, post=lambda _t: None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# End to end: rolling retrain on a synthetic 2020-2025 ledger (real LightGBM)
# ---------------------------------------------------------------------------


def _ledger_through(root: Path, last: dt.date, data_end: dt.date) -> tuple[Path, Path]:
    """The E27.3 synthetic store (tests/test_ml_m2_model) extended past 2024 + manifest."""
    from arc.ml.features.registry import schema_version
    from arc.ml.m1.train import model_features
    from arc.utils.calendar import sessions_between

    rng = np.random.default_rng(11)
    days = sessions_between(dt.date(2020, 1, 1), last)[::4]
    ledger = root / "ml" / "ledger" / "v1"
    fdir = root / "ml" / "features" / f"v{schema_version()}"
    fdir.mkdir(parents=True)
    pcols = model_features()
    by: dict[tuple[int, str], list[dict[str, Any]]] = {}
    tickers = ("AAA", "BBB", "ETF1")
    for d in days:
        panel = {"date": [d] * 3, "ticker": list(tickers)}
        for c in pcols:
            panel[c] = rng.normal(size=3)
        pd.DataFrame(panel).to_parquet(fdir / f"date={d.isoformat()}.parquet", index=False)
        for t in tickers:
            for i in range(10):
                pop = float(rng.uniform(0.2, 0.8))
                win = rng.uniform() < pop
                end = d + dt.timedelta(days=int(rng.integers(5, 40)))
                ml = 200.0
                by.setdefault((d.year, t), []).append(
                    {
                        "date": d, "ticker": t, "key": f"{t}|{d}|{i}",
                        "kind": ("bull_call", "bear_put", "long_call")[i % 3],
                        "sleeve": "swing", "direction": "bull" if i % 2 else "bear",
                        "credit": False, "dte": 30.0, "anchor_delta": 0.4, "width_pct": 0.05,
                        "static_pop": pop, "static_net_ev": (pop - 0.5) * ml,
                        "managed_pop": pop, "managed_net_ev": (pop - 0.5) * ml,
                        "ev_proxy": (pop - 0.5) * ml, "net_mid": 2.0, "max_loss_mid": ml,
                        "max_gain_mid": ml, "est_cost": 4.0, "expected_days_held": 10.0,
                        "be_atr": 0.5, "be_sigma": 0.5, "spread_to_premium": 0.05,
                        "leg_oi_min": 500.0, "leg_volume_min": 50.0, "shorts_sellable": 1.0,
                        "credit_width": np.nan, "debit_width": 0.3, "rorc_day": 0.01,
                        "vrp": 0.0, "ctx_trend": "up", "ctx_vol": "normal",
                        "ctx_filter_reason": "", "ctx_passes_filters": True,
                        "ctx_credit_only": False, "ctx_entry_spread_ok": True,
                        "ctx_earnings_blackout": False, "ctx_regime_blocked": False,
                        "ctx_liquidity": 1.0, "ctx_etf": t == "ETF1",
                        "label_win_x025": bool(win),
                        "label_r_net_x025": (0.8 if win else -0.6) + 0.1 * rng.normal(),
                        "label_hit_stop": bool(not win and rng.uniform() < 0.5),
                        "label_days_held": float(rng.integers(1, 20)),
                        "label_exit_reason": "tp" if win else "stop",
                        "label_end_date": end,
                        "label_crosses_holdout": d < dt.date(2024, 1, 1) <= end,
                        "n_concurrent": 1, "sample_weight": 1.0, "spec": "{}",
                        "split": "dev" if d < dt.date(2024, 1, 1) else "holdout",
                    }
                )  # fmt: skip
    for (y, t), rows in by.items():
        p = ledger / "profile=cash_debit" / f"year={y}"
        p.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(p / f"part-{t}.parquet", index=False)
    (ledger / "manifest.json").write_text(
        json.dumps({"end": last.isoformat(), "data_end": data_end.isoformat()})
    )
    return ledger, fdir.parent


@pytest.mark.slow
def test_run_retrain_end_to_end_registers_candidate_and_logs_rolling_read(
    tmp_path: Path,
) -> None:
    require_lightgbm()
    from arc.ml.m2.config import GridPoint, M2Config
    from arc.ml.m2.train import train_family

    ledger, froot = _ledger_through(tmp_path, dt.date(2025, 8, 15), dt.date(2025, 10, 15))
    cfg = M2Config(
        grid=[GridPoint(num_leaves=7, min_data_in_leaf=20)],
        max_rounds=30,
        early_stopping=5,
        cap_per_day=None,
        num_threads=2,
    )
    models = tmp_path / "ml" / "models"
    # the serving incumbent: an E27.3-style dev model (trained through 2023), shadow
    inc = train_family(
        "debit", cfg, ledger_root=ledger.parent, features_root=froot, out_root=models,
        code_sha="t", now=NOW, monotone_check=False,
    )  # fmt: skip
    reg.register(models, "debit", inc.version, now=NOW, rule="m2_promote", rule_passed=True)

    res = rt.run_retrain(
        data_dir=tmp_path,
        now=NOW,
        code_sha="t",
        families=["debit"],
        cfg=cfg,
        retrain_m1=False,
        monotone_check=False,
    )
    w = res.window
    assert w is not None
    assert w.gap_days >= rt.MIN_GAP_DAYS
    assert w.holdout_end <= dt.date(2025, 8, 15)
    assert (w.holdout_start - w.train_end).days > w.gap_days
    fam = res.families["debit"]
    assert fam.error is None, fam.error
    assert fam.incumbent == inc.version and fam.version != inc.version
    assert fam.n_holdout > rt.MIN_N
    assert fam.gate["status"] in {"pass", "fail"}

    # candidate only, never auto-shadow (even on a pass); the incumbent keeps serving
    idx = reg.load_index(models, "debit")
    assert idx.versions[fam.version].status == "candidate"
    assert idx.versions[inc.version].status == "shadow"
    # the new model trained past 2023 but never on the holdout or the gap
    man = json.loads((models / "debit" / fam.version / "manifest.json").read_text())
    assert man["train_window"]["end"] == w.train_end.isoformat()
    assert man["retrain"]["kind"] == "rolling"
    oos = pd.read_parquet(models / "debit" / fam.version / "oos_scores.parquet")
    assert oos["date"].max() <= w.train_end
    assert oos["date"].max() >= dt.date(2024, 1, 1)
    # one rolling read with window, gap, n and the both-models metrics
    (read,) = reg.rolling_reads(models, "debit")
    assert read["reason"] == rt.REASON and read["version"] == fam.version
    assert read["gap_days"] == w.gap_days and read["n"] == fam.n_holdout
    assert {m["model"] for m in read["metrics"]} == {"candidate", "incumbent"}
    assert reg.holdout_reads(models, "debit") == []  # the D84 one-time read is untouched
    slices = json.loads((models / "debit" / fam.version / "rolling_holdout.json").read_text())
    assert all("n" in s for s in slices["slices"])
    assert "candidate only" in res.summary()


def test_m1_dataset_crosses_the_d84_boundary_only_with_a_reason(tmp_path: Path) -> None:
    from arc.ml.m1 import dataset as ds

    with pytest.raises(ds.HoldoutReadError):
        ds.load_panel(tmp_path, dt.date(2023, 12, 1), dt.date(2024, 2, 1))
    with pytest.raises(ds.HoldoutReadError):
        ds.attach_targets(pd.DataFrame(), tmp_path, dev_end=dt.date(2024, 6, 30))
    with pytest.raises(FileNotFoundError):  # past the guard: only the empty store remains
        ds.load_panel(tmp_path, dt.date(2023, 12, 1), dt.date(2024, 2, 1), holdout_reason=rt.REASON)


def test_retrain_child_runs_reniced_with_libomp_env_and_parses_the_report(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from arc.ml.m2.train import bundled_libomp_dir

    calls: list[dict[str, Any]] = []
    report = {"summary": "ml.retrain: …", "metrics": {"families": {"debit": {"error": "x"}}}}

    def run(cmd: list[str], **kw: Any) -> Any:
        calls.append({"cmd": cmd, **kw})
        return SimpleNamespace(returncode=0, stdout="noise\n" + json.dumps(report), stderr="")

    out = rt.retrain_in_child(
        repo=tmp_path, data_dir=tmp_path / "d", code_sha="abc", families=["credit", "debit"],
        months=6, min_gap_days=45, env={"PATH": "/bin"}, timeout_s=60, runner=run,
    )  # fmt: skip
    (c,) = calls
    assert c["cmd"][1:5] == ["-m", "arc.cli", "ml", "retrain"] and "--json" in c["cmd"]
    assert callable(c["preexec_fn"])
    assert c["cmd"][c["cmd"].index("--families") + 1] == "credit,debit"
    assert c["timeout"] == 60 and c["env"]["PATH"] == "/bin"  # noqa: PLR2004
    if bundled_libomp_dir():
        assert c["env"]["DYLD_LIBRARY_PATH"] == bundled_libomp_dir()
    assert out.summary() == "ml.retrain: …" and out.errors() == {"debit": "x"}

    def boom(cmd: list[str], **kw: Any) -> Any:
        return SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nImportError: x")

    with pytest.raises(RuntimeError, match="exited 1 without a report: Traceback"):
        rt.retrain_in_child(
            repo=tmp_path, data_dir=tmp_path, code_sha="a", families=["credit"], months=6,
            min_gap_days=45, env={}, timeout_s=1, runner=boom,
        )  # fmt: skip


def test_load_lightgbm_names_the_libomp_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import sys

    from arc.ml.m2 import train as m2t

    real = builtins.__import__

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name == "lightgbm":
            raise OSError("dlopen(lib_lightgbm.dylib): Library not loaded: @rpath/libomp.dylib")
        return real(name, *a, **k)

    monkeypatch.delitem(sys.modules, "lightgbm", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ImportError, match="brew install libomp"):
        m2t.load_lightgbm()
    assert m2t.lightgbm_env({"DYLD_LIBRARY_PATH": "/x"})["DYLD_LIBRARY_PATH"].startswith("/x")


def test_m1_failure_is_reported_and_m2_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.ml.m1 import train as m1t
    from arc.ml.m2 import train as m2t

    _ledger_through(tmp_path, dt.date(2025, 8, 15), dt.date(2025, 10, 15))

    def no_m1(*_a: Any, **_k: Any) -> Any:
        raise ValueError("No objects to concatenate")

    trained: list[str] = []

    def fake_family(family: str, *_a: Any, **_k: Any) -> Any:
        trained.append(family)
        raise RuntimeError("stop here")

    monkeypatch.setattr(m1t, "train_m1", no_m1)
    monkeypatch.setattr(m2t, "train_family", fake_family)
    res = rt.run_retrain(data_dir=tmp_path, now=NOW, code_sha="t", families=["debit"])
    assert res.m1_version is None and res.m1_error == "ValueError: No objects to concatenate"
    assert trained == ["debit"] and "M1 retrain failed" in res.summary()
    assert res.metrics()["m1_error"] == res.m1_error
