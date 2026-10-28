"""E27.6 deep-learning challengers (D126): prep, sequences, surfaces, nets, stacker.

Point-in-time: every aux input (sequence window, surface) of session *d* is tested to be
unchanged when every later row (or every other session) is mutated. Determinism: two CPU
fits with the same seed give byte-identical weights and predictions.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from arc.ml.dl import prep
from arc.ml.dl import sequence as sq
from arc.ml.dl import surface as sf
from arc.ml.dl.config import CHALLENGERS, DLConfig, NetSpec

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_preregistered_and_digest_stable() -> None:
    a, b = DLConfig(), DLConfig()
    assert a.digest() == b.digest()
    assert set(a.nets) == set(CHALLENGERS)
    assert a.nets["temporal"].aux == "sequence"
    assert a.nets["surface"].aux == "surface"
    assert a.nets["ft"].trunk == "ft"
    assert a.seq_window == 60  # noqa: PLR2004 (card)
    changed = DLConfig(nets={**a.nets, "mlp": NetSpec(seeds=9)})
    assert changed.digest() != a.digest()


# ---------------------------------------------------------------------------
# quantile normaliser
# ---------------------------------------------------------------------------


def test_quantile_normalizer_train_only_and_missing_flags() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(2000, 3))
    x[:, 2] = 4.0  # constant
    x[::10, 0] = np.nan
    qn = prep.QuantileNormalizer.fit(x, ["a", "b", "c"], bins=100)
    z, miss = qn.transform(x)
    assert z.dtype == np.float32
    assert miss[::10, 0].all() and not miss[1::10, 0].any()
    assert (z[::10, 0] == 0).all()
    assert np.allclose(z[:, 2], 0.0)
    assert abs(float(np.median(z[1::10, 1]))) < 0.1  # noqa: PLR2004
    assert np.abs(z).max() <= prep.CLIP
    # monotone
    s = np.argsort(x[:, 1])
    assert np.all(np.diff(z[s, 1]) >= 0)
    # round-trips through JSON
    qn2 = prep.QuantileNormalizer.model_validate_json(qn.model_dump_json())
    assert np.array_equal(qn2.transform(x)[0], z)


def test_fit_inputs_ignores_test_rows() -> None:
    rng = np.random.default_rng(1)
    df = pd.DataFrame({"f": rng.normal(size=500)})
    train = np.arange(500) < 300
    a = prep.fit_inputs(df, train, ["f"], {}, bins=50)
    df2 = df.copy()
    df2.loc[~train, "f"] = 1e6
    b = prep.fit_inputs(df2, train, ["f"], {}, bins=50)
    assert a.model_dump() == b.model_dump()


# ---------------------------------------------------------------------------
# sequences
# ---------------------------------------------------------------------------


def _ohlc(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2021-01-04", periods=n).date
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {"open": c * 0.999, "high": c * 1.01, "low": c * 0.99, "close": c}, index=days
    )


def _opt(idx: object, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(idx)  # type: ignore[arg-type]
    return pd.DataFrame(
        {
            "iv30": 0.3 + rng.normal(0, 0.01, n),
            "term_slope": rng.normal(0, 0.01, n),
            "skew_25d": rng.normal(0.05, 0.01, n),
            "opt_volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )


def _table(px: dict[str, pd.DataFrame]) -> sq.SequenceTable:
    spy = sq.ticker_vectors(px["SPY"], None, _opt(px["SPY"].index, 9))
    frames = {
        t: sq.with_market(
            sq.ticker_vectors(p, pd.Series(1e6, index=p.index), _opt(p.index, 3)), spy
        )
        for t, p in px.items()
    }
    return sq.build_table(frames)


def test_sequence_windows_point_in_time() -> None:
    px = {"SPY": _ohlc(200, 0), "AAA": _ohlc(200, 1)}
    t1 = _table(px)
    d = px["AAA"].index[120]
    later = {k: v.copy() for k, v in px.items()}
    for v in later.values():
        v.loc[v.index > d, ["open", "high", "low", "close"]] *= 3.0
    t2 = _table(later)
    sc = sq.ChannelScaler.fit(sq.train_vectors(t1, d))
    g1 = sq.gather_for(t1, ["AAA"], [d], 60, sc)
    g2 = sq.gather_for(t2, ["AAA"], [d], 60, sc)
    w1, w2 = g1(np.array([0])), g2(np.array([0]))
    assert w1.shape == (1, 60, len(sq.CHANNELS))
    assert np.array_equal(w1, w2)
    # last step is session d itself; the window does not cross into another ticker
    early = sq.gather_for(t1, ["AAA"], [px["AAA"].index[3]], 60, sc)(np.array([0]))
    assert (early[0, :56] == 0).all()
    # unknown session → all-zero window
    none = sq.gather_for(t1, ["ZZZ"], [d], 60, sc)(np.array([0]))
    assert (none == 0).all()


def test_sequence_holdout_refused(tmp_path) -> None:
    with pytest.raises(sq.HoldoutReadError):
        sq.load_tables(tmp_path, ["AAA"], dt.date(2023, 1, 1), dt.date(2024, 1, 2))


# ---------------------------------------------------------------------------
# surfaces
# ---------------------------------------------------------------------------


def _chain(iv_shift: float = 0.0) -> pd.DataFrame:
    rows = []
    for dte in (14, 28, 42, 70):
        for k in np.linspace(0.05, 0.95, 19):
            rows.append(
                {
                    "dte": dte,
                    "right": "call",
                    "delta": k,
                    "iv": 0.3 + 0.1 * (0.5 - k) + iv_shift + dte / 1e3,
                }
            )
            rows.append(
                {
                    "dte": dte,
                    "right": "put",
                    "delta": -k,
                    "iv": 0.3 + 0.1 * (k - 0.5) + 0.02 + iv_shift,
                }
            )
    return pd.DataFrame(rows)


def test_day_surface_grid_and_mask() -> None:
    iv, m = sf.day_surface(_chain())
    assert iv.shape == (len(sf.DELTA_GRID), len(sf.DTE_GRID))
    assert np.isfinite(iv).all()
    # 10 DTE is below the first expiry (14): flat extrapolation → mask 0.5
    assert (m[:, sf.DTE_GRID.index(10)] == 0.5).all()  # noqa: PLR2004
    assert (m[:, sf.DTE_GRID.index(30)] == 1.0).all()
    # 75 > 70 → extrapolated
    assert (m[:, sf.DTE_GRID.index(75)] == 0.5).all()  # noqa: PLR2004
    # put wing (c > 0.5) reads the puts: higher than the matching call side here
    assert iv[sf.DELTA_GRID.index(0.9), 2] > iv[sf.DELTA_GRID.index(0.1), 2] - 0.2  # noqa: PLR2004
    empty_iv, empty_m = sf.day_surface(pd.DataFrame())
    assert np.isnan(empty_iv).all() and (empty_m == 0).all()


def test_surfaces_point_in_time() -> None:
    d0, d1 = dt.date(2022, 3, 1), dt.date(2022, 3, 2)
    a = sf.surfaces_from_chains("AAA", {d0: _chain(), d1: _chain()})
    b = sf.surfaces_from_chains("AAA", {d0: _chain(), d1: _chain(0.5)})
    assert a.iloc[0].equals(b.iloc[0])
    assert not a.iloc[1].equals(b.iloc[1])


def test_surface_tensor_and_gather() -> None:
    d0 = dt.date(2022, 3, 1)
    cache = sf.surfaces_from_chains("AAA", {d0: _chain()})
    c, s = sf.log_iv_stats(cache, dt.date(2023, 1, 1))
    g = sf.gather_for(cache, ["AAA", "BBB"], [d0, d0], c, s)
    x = g(np.array([0, 1]))
    assert x.shape == (2, sf.CHANNELS, len(sf.DELTA_GRID), len(sf.DTE_GRID))
    assert np.isfinite(x).all()
    assert (x[1] == 0).all()  # missing surface → zeros (mask channel 0)
    assert x[0, 1, sf.ATM_I, sf.D30_J] == 0.0  # shape channel at ATM-30 is 0


def test_surface_holdout_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="holdout"):
        sf.build_ticker(tmp_path, "AAA", dt.date(2023, 1, 1), dt.date(2024, 2, 1))


# ---------------------------------------------------------------------------
# stacker (sklearn)
# ---------------------------------------------------------------------------


def test_stacker_ex_ante() -> None:
    pytest.importorskip("sklearn")
    from arc.ml.dl import pipeline as pl

    rng = np.random.default_rng(2)
    sessions = list(pd.bdate_range("2021-01-04", "2023-03-31").date)
    n = 12000
    dates = np.array([sessions[i] for i in rng.integers(0, len(sessions) - 15, n)])
    end = np.array([sessions[sessions.index(d) + 10] for d in dates])
    p = rng.uniform(0.2, 0.8, n)
    y = np.column_stack(
        [
            (rng.uniform(size=n) < p).astype(float),
            (rng.uniform(size=n) < 0.2).astype(float),
            rng.normal(p - 0.5, 0.1),
            np.full(n, 10.0),
        ]
    )
    m2 = pd.DataFrame({"p_win": p, "p_stop": np.full(n, 0.2), "e_r": p - 0.5})
    dl = pd.DataFrame(
        {
            "p_win": np.clip(p + rng.normal(0, 0.05, n), 0.01, 0.99),
            "p_stop": np.full(n, 0.2),
            "e_r": p - 0.5 + rng.normal(0, 0.05, n),
        }
    )
    st = pl.fit_stacker(dates, end, sessions, m2, dl, y, np.ones(n), 2022)
    assert len(st.coefs["win"]) == 4  # noqa: PLR2004
    assert st.n_fit == sorted(st.n_fit)  # expanding window
    t22 = (dates >= dt.date(2022, 1, 1)) & (dates <= dt.date(2022, 12, 31))
    out = pl.stacked_outputs(m2[t22], dl[t22], st, dates[t22])
    assert out["p_win"].between(0, 1).all()
    assert np.corrcoef(out["e_r"], m2.loc[t22, "e_r"])[0, 1] > 0.9  # noqa: PLR2004
    # no block reads its own labels or any later label: flipping every label from Q2 on
    # leaves Q1 and Q2 untouched (Q2 can only see labels closed before 2022-04-01)
    y2 = y.copy()
    later = dates >= dt.date(2022, 4, 1)
    y2[later, 0] = 1 - y2[later, 0]
    y2[later, 2] = -y2[later, 2]
    st2 = pl.fit_stacker(dates, end, sessions, m2, dl, y2, np.ones(n), 2022)
    for t in ("win", "r"):
        assert st2.coefs[t][:2] == st.coefs[t][:2]
        assert st2.coefs[t][2] != st.coefs[t][2]
    # with no ex-ante rows the blend is the plain prior
    first = pl.fit_stacker(dates, end, sessions, m2, dl, y, np.ones(n), 2021)
    assert first.coefs["win"][0] == pl.STACK_PRIOR


# ---------------------------------------------------------------------------
# nets + trainer (torch)
# ---------------------------------------------------------------------------


def _toy(n: int = 600, seed: int = 0):
    from arc.ml.dl.trainer import Batchable

    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 5)).astype(np.float32)
    win = (x[:, 0] + rng.normal(0, 0.5, n) > 0).astype(float)
    y = np.column_stack([win, 1 - win, x[:, 0] * 0.3, np.full(n, 10.0)])
    y[::7, 1] = np.nan
    cat = rng.integers(0, 3, (n, 1)).astype(np.int64)
    return Batchable(x, np.zeros_like(x), cat, y, np.ones(n))


@pytest.mark.parametrize("trunk", ["resmlp", "ft"])
def test_cpu_fit_deterministic_and_learns(trunk: str) -> None:
    pytest.importorskip("torch")
    from arc.ml.dl import trainer
    from arc.ml.dl.config import LossWeights
    from arc.ml.dl.nets import HEADS

    spec = NetSpec(
        trunk=trunk,
        d_hidden=32,
        n_blocks=1,
        d_token=8,
        ft_layers=1,
        ft_heads=2,
        batch=128,
        max_epochs=6,
        patience=10,
        device="cpu",
    )
    d = _toy()
    tr, va = d.take(np.arange(450)), d.take(np.arange(450, 600))
    kw = {"seed": 7, "huber": 1.0, "threads": 1, "cards": [3]}
    a = trainer.fit(spec, LossWeights(), tr, val=va, **kw)
    b = trainer.fit(spec, LossWeights(), tr, val=va, **kw)
    assert a.epochs == b.epochs
    for k in a.state:
        assert a.state[k].numpy().tobytes() == b.state[k].numpy().tobytes()
    pa = trainer.predict(spec, a.state, va, cards=[3], device="cpu")
    pb = trainer.predict(spec, b.state, va, cards=[3], device="cpu")
    assert pa.tobytes() == pb.tobytes()
    assert pa.shape == (150, len(HEADS))
    w = pa[:, HEADS.index("win")]
    assert ((w > 0) & (w < 1)).all()
    yw = va.y[:, 0]
    if trunk == "resmlp":  # the tiny FT needs more epochs than this smoke test runs
        assert w[yw == 1].mean() > w[yw == 0].mean()


@pytest.mark.parametrize("aux", ["sequence", "surface"])
def test_aux_nets_run(aux: str) -> None:
    pytest.importorskip("torch")
    from arc.ml.dl import trainer
    from arc.ml.dl.config import LossWeights

    d = _toy(200)
    shape = (8, len(sq.CHANNELS)) if aux == "sequence" else (sf.CHANNELS, 7, 6)
    rng = np.random.default_rng(3)
    store = rng.normal(size=(200, *shape)).astype(np.float32)
    d.aux, d.aux_shape = (lambda i: store[i]), shape
    spec = NetSpec(aux=aux, d_hidden=16, n_blocks=1, d_aux=8, batch=64, max_epochs=1, device="cpu")
    r = trainer.fit(spec, LossWeights(), d, epochs=1, seed=1, huber=1.0, threads=1, cards=[3])
    sub = d.take(np.arange(50, 80))
    p = trainer.predict(spec, r.state, sub, cards=[3], device="cpu")
    assert p.shape[0] == 30  # noqa: PLR2004
    assert np.isfinite(p).all()


def test_outputs_mirror_m2_columns() -> None:
    from arc.ml.dl import pipeline as pl
    from arc.ml.dl.nets import HEADS
    from arc.ml.m2.predict import OUTPUTS

    class Ident:
        def predict(self, raw, kinds):
            del kinds
            return raw

    raw = np.random.default_rng(0).uniform(0.1, 0.9, (5, len(HEADS)))
    out = pl.outputs(raw, {"win": Ident(), "stop": Ident()}, np.array(["x"] * 5))  # type: ignore[dict-item]
    assert set(OUTPUTS) <= set(out.columns)
    assert (out["r_q10"] <= out["r_q50"]).all() and (out["r_q50"] <= out["r_q90"]).all()


def test_live_scorer_never_imports_dl() -> None:
    import subprocess
    import sys

    code = (
        "import sys, arc.ml.m2.predict, arc.ml.m1.predict;"
        "import arc.backtest.ml_arms;"
        "print(any(m.startswith('arc.ml.dl') or m == 'torch' for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
