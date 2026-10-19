"""E7.21 (D125, D126): the candidate outcome ledger (arc.ml.ledger).

* the look-ahead mutation test: rows after d multiplied → features unchanged, labels move;
* leg sharing (one ``LegBook`` + one walk, re-priced per x) equals the naive
  per-candidate ``sim_outcome`` at every x;
* the holdout refusal (D84) and the deterministic, hash-based sample;
* ``n_concurrent`` overlap counts.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from arc.backtest.sim import profile_sleeves, sim_outcome, sleeve_menus, ticker_prep
from arc.ml.ledger.build import (
    DEFAULT_XS,
    HoldoutRefusedError,
    LedgerInputs,
    Sampling,
    load_ledger,
    n_concurrent,
    split_of,
    ticker_rows,
    xtag,
)
from arc.ml.ledger.outcomes import LegBook, candidate_rules, price_labels, settle_spot, walk
from arc.scanner.rank import Ranker
from tests.test_backtest_sim import (
    COST,
    DAYS,
    EXITS,
    _cfg,
    _days,
    _settings,
    view,  # noqa: F401 - module fixture
)

if TYPE_CHECKING:
    from pathlib import Path

    from arc.backtest.data import HistoryView

D0 = DAYS[2]
WINDOW = (20, 45)


def _inputs(profile: str = "margin") -> LedgerInputs:
    s = _settings(profile)
    return LedgerInputs(
        cfg=_cfg(),
        exits=EXITS,
        cost=COST,
        settings={profile: s},
        windows={profile: WINDOW},
        rankers={profile: [Ranker.MANAGED_NET_EV]},
        stances=None,
        sampling=Sampling(n=1, credit_band=(0.15, 0.35), debit_band=(0.4, 0.7)),
        range_rule=None,
    )


def _rows(
    chains: dict[dt.date, pd.DataFrame], closes: pd.Series, days: list[dt.date]
) -> pd.DataFrame:
    out = ticker_rows(
        "AAA",
        chains=chains,
        closes=closes,
        ohlc=None,
        days=days,
        info=_days(100.0),
        inp=_inputs(),
        rate=None,
        quotes=None,
        splits=None,
        trends=None,
    )
    return pd.DataFrame(out["margin"])


def test_rows_cover_every_menu_kind_and_carry_labels(view: HistoryView) -> None:  # noqa: F811
    h = view.load("AAA")
    df = _rows(h.chains, h.closes, [D0])
    assert len(df) > 0
    assert set(df["date"]) == {D0}
    # margin is the superset: credit, debit, long premium and condors
    assert {"bull_put", "bear_call", "bull_call", "long_call"} <= set(df["kind"])
    for x in DEFAULT_XS:
        assert df[f"label_r_net_{xtag(x)}"].notna().all()
    # more slippage never helps a candidate
    assert (df["label_pnl_x000"] >= df["label_pnl_x025"] - 1e-9).all()
    assert (df["label_pnl_x025"] >= df["label_pnl_x050"] - 1e-9).all()
    assert (df["label_end_date"] > df["date"]).all()
    assert (df["split"] == "holdout").all()  # the fixture is 2025 (D84 holdout)


def test_look_ahead_mutation_changes_labels_only(view: HistoryView) -> None:  # noqa: F811
    """Multiply every quote and close after d: features stay, labels move."""
    h = view.load("AAA")
    base = _rows(h.chains, h.closes, [D0])
    chains2 = {}
    for d, df in h.chains.items():
        if d > D0:
            df = df.copy()
            for col in ("mid", "bid", "ask", "spread"):
                if col in df:
                    df[col] = df[col] * 1.7
            chains2[d] = df
        else:
            chains2[d] = df
    closes2 = h.closes.copy()
    closes2[closes2.index > D0] *= 1.25
    mut = _rows(chains2, closes2, [D0])
    assert len(mut) == len(base)
    keys = ["key", "profile", "sleeve"]
    base = base.sort_values(keys).reset_index(drop=True)
    mut = mut.sort_values(keys).reset_index(drop=True)
    feats = [c for c in base.columns if not c.startswith("label_")]
    pd.testing.assert_frame_equal(base[feats], mut[feats])
    labels = [c for c in base.columns if c.startswith("label_pnl_")]
    moved = (base[labels] - mut[labels]).abs().to_numpy().max()
    assert moved > 1.0


def test_leg_sharing_equals_naive_sim_outcome(view: HistoryView) -> None:  # noqa: F811
    """One LegBook + one walk per candidate, re-priced per x == sim_outcome per x."""
    h = view.load("AAA")
    cfg = _cfg()
    s = _settings("margin")
    sim = cfg.backtest.sim
    days = [DAYS[1], DAYS[4]]
    prep = ticker_prep(
        h.chains, h.closes, decision_days=days, cfg=cfg, exits=EXITS, cost=COST, sim=sim
    )
    menus: list = []
    for sw in profile_sleeves(s, WINDOW, sim):
        m, _ = sleeve_menus(
            "AAA",
            h.chains,
            h.closes,
            sw=sw,
            profile="margin",
            dd=prep.dd,
            cfg=cfg,
            exits=EXITS,
            mc=prep.mc,
            base=prep.base,
            rate=None,
            day_stance=prep.day_stance,
            cache=None,
            cache_ctx=None,
        )
        menus.extend(c for cs in m.values() for c in cs)
    assert len(menus) > 20
    legs = {p.symbol: (p.right, p.strike, p.expiration) for c in menus for p in c.picks}
    book = LegBook(h.chains, legs, h.closes)
    checked = 0
    for c in menus:
        w = walk(c, candidate_rules(c, s, EXITS), book, settings=s, spread_rule=prep.spread_rule)
        ss = settle_spot(c, h.closes)
        assert ss is not None
        for x in DEFAULT_XS:
            cost = COST.model_copy(update={"slippage_frac": x})
            lab = price_labels(c, w, spot_entry=float(h.closes[c.day]), spot_settle=ss, cost=cost)
            res = sim_outcome(
                c,
                chains=h.chains,
                closes=h.closes,
                cost=cost,
                exits=EXITS,
                settings=s,
                spread_rule=prep.spread_rule,
            )
            if res is None:
                assert lab is None
                continue
            assert lab is not None
            tr = res.outcome.trade
            assert lab.pnl == pytest.approx(tr.pnl, abs=1e-9)
            assert lab.max_loss == pytest.approx(tr.max_loss, abs=1e-9)
            assert lab.exit_reason == str(tr.exit_reason)
            assert lab.exit_day == res.outcome.exit_day
            assert lab.hold_pnl == pytest.approx(res.outcome.hold_to_expiry_pnl, abs=1e-9)
            checked += 1
    assert checked > 60


# ---------------------------------------------------------------------------
# reader, sampling, overlap
# ---------------------------------------------------------------------------


def _write(tmp: Path) -> Path:
    v = tmp / "v1"
    rows = [
        {"date": dt.date(2023, 12, 29), "ticker": "AAA", "split": "dev", "key": "a"},
        {"date": dt.date(2024, 1, 2), "ticker": "AAA", "split": "holdout", "key": "b"},
    ]
    for r in rows:
        p = v / "profile=margin" / f"year={r['date'].year}" / "part-AAA.parquet"  # type: ignore[union-attr]
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([r]).to_parquet(p, index=False)
    return tmp


def test_holdout_refused_unless_allowed_with_reason(tmp_path: Path) -> None:
    root = _write(tmp_path)
    dev = load_ledger(root)
    assert list(dev["key"]) == ["a"]
    with pytest.raises(HoldoutRefusedError):
        load_ledger(root, split="holdout")
    with pytest.raises(HoldoutRefusedError):
        load_ledger(root, split="all", allow_holdout=True)
    with pytest.raises(HoldoutRefusedError):
        load_ledger(root, split="holdout", reason="no flag")
    got = load_ledger(root, split="holdout", allow_holdout=True, reason="E27.3 final eval")
    assert list(got["key"]) == ["b"]
    assert len(load_ledger(root, split="all", allow_holdout=True, reason="audit")) == 2
    with pytest.raises(ValueError, match="split"):
        load_ledger(root, split="test")


def test_split_boundary_is_d84() -> None:
    assert split_of(dt.date(2023, 12, 31)) == "dev"
    assert split_of(dt.date(2024, 1, 1)) == "holdout"


def test_sample_is_deterministic_and_keeps_every_in_band_row() -> None:
    s = Sampling(n=4, credit_band=(0.15, 0.35), debit_band=(0.4, 0.7))
    day = dt.date(2021, 3, 1)
    # in band: always kept, weight 1
    assert s.weight("AAA", day, "k", "bull_put", 0.25) == 1
    assert s.weight("AAA", day, "k", "long_call", 0.5) == 1
    keys = [f"key{i}" for i in range(4000)]
    w1 = [s.weight("AAA", day, k, "bull_put", 0.05) for k in keys]
    w2 = [s.weight("AAA", day, k, "bull_put", 0.05) for k in keys]
    assert w1 == w2  # hash, never random
    kept = [w for w in w1 if w]
    assert set(kept) == {4}  # inverse-probability weight
    assert 0.2 < len(kept) / len(keys) < 0.3
    # another day re-draws the off-band sample
    w3 = [s.weight("AAA", day + dt.timedelta(days=1), k, "bull_put", 0.05) for k in keys]
    assert w3 != w1
    assert (
        Sampling(n=1, credit_band=(0, 1), debit_band=(0, 1)).weight(
            "AAA", day, "k", "bull_put", math.nan
        )
        == 1
    )


def test_n_concurrent_counts_overlapping_label_windows() -> None:
    d = dt.date
    df = pd.DataFrame(
        {
            "ticker": ["A", "A", "A", "B"],
            "profile": ["m", "m", "m", "m"],
            "date": [d(2021, 1, 4), d(2021, 1, 5), d(2021, 2, 1), d(2021, 1, 4)],
            "label_end_date": [d(2021, 1, 20), d(2021, 1, 8), d(2021, 2, 9), d(2021, 3, 1)],
        }
    )
    assert n_concurrent(df).tolist() == [1, 1, 0, 0]
