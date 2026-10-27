"""M2 dataset (E27.3): ledger rows × the E27.1 feature store, scale-free candidate features.

Rows: one E7.21 ledger profile (``credit`` family = margin, ``debit`` = cash_debit),
dev split only unless the caller passes the holdout authorisation through
:func:`load_family` (the registry logs every holdout read). Rows whose label window
crosses the holdout boundary are dropped from dev (their labels read 2024 prices).

Features (``No ticker id``, card):

* the panel features of :func:`arc.ml.m1.train.model_features` (the feature store
  version must equal the running ``FEATURE_SCHEMA_VERSION``);
* candidate features derived from the ledger row (:data:`CAND_NUMERIC`,
  :data:`CAND_CATEGORICAL`): kind / sleeve / direction / trend / vol, the D128 filter
  code (``ctx_filter_reason``) and ``credit_only``, the D131 regime verdict, and money
  values divided by the structure's max loss so credit and debit share one scale
  (credit R = P&L ÷ max loss, card).

M1 outputs are not added: the registered M1 (``m1-53833784f276``) kept the baseline
source for every target, whose inputs (``hq_*``, ``har_rv_*``, IV30) are already panel
features, and M1 has no point-in-time (out-of-fold) values before 2022.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from arc.ml import validate as v

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence
    from pathlib import Path

LEDGER_ID = ("date", "ticker", "key", "kind", "sleeve")
CAND_CATEGORICAL: tuple[str, ...] = (
    "kind",
    "sleeve",
    "direction",
    "ctx_trend",
    "ctx_vol",
    "ctx_filter_reason",
)
CAND_NUMERIC: tuple[str, ...] = (
    "credit",
    "dte",
    "anchor_delta",
    "width_pct",
    "static_pop",
    "managed_pop",
    "static_ev_per_risk",
    "managed_ev_per_risk",
    "ev_proxy_per_risk",
    "net_per_risk",
    "gain_per_risk",
    "cost_per_risk",
    "expected_days_held",
    "be_atr",
    "be_sigma",
    "cand_spread_over_premium",
    "log_leg_oi_min",
    "log_leg_volume_min",
    "shorts_sellable",
    "credit_width",
    "debit_width",
    "rorc_day",
    "vrp",
    "log_max_loss",
    "ctx_passes_filters",
    "ctx_credit_only",
    "ctx_entry_spread_ok",
    "ctx_earnings_blackout",
    "ctx_regime_blocked",
    "ctx_liquidity",
    "ctx_etf",
)
#: Raw ledger columns the derivations read.
LEDGER_FEATURE_COLS: tuple[str, ...] = (
    "direction",
    "credit",
    "dte",
    "anchor_delta",
    "width_pct",
    "static_pop",
    "static_net_ev",
    "managed_pop",
    "managed_net_ev",
    "ev_proxy",
    "net_mid",
    "max_loss_mid",
    "max_gain_mid",
    "est_cost",
    "expected_days_held",
    "be_atr",
    "be_sigma",
    "spread_to_premium",
    "leg_oi_min",
    "leg_volume_min",
    "shorts_sellable",
    "credit_width",
    "debit_width",
    "rorc_day",
    "vrp",
    "ctx_trend",
    "ctx_vol",
    "ctx_filter_reason",
    "ctx_passes_filters",
    "ctx_credit_only",
    "ctx_entry_spread_ok",
    "ctx_earnings_blackout",
    "ctx_regime_blocked",
    "ctx_liquidity",
    "ctx_etf",
)


def label_cols(x: str) -> dict[str, str]:
    """Model target -> ledger column at label slippage *x* (``x025``)."""
    return {
        "win": f"label_win_{x}",
        "r": f"label_r_net_{x}",
        "stop": "label_hit_stop",
        "days": "label_days_held",
    }


def ledger_columns(x: str) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *LEDGER_ID,
                *LEDGER_FEATURE_COLS,
                *label_cols(x).values(),
                "label_exit_reason",
                "label_end_date",
                "label_crosses_holdout",
                "n_concurrent",
                "sample_weight",
                "spec",
            ]
        )
    )


def _ratio(num: pd.Series, den: pd.Series) -> np.ndarray:
    n = pd.to_numeric(num, errors="coerce").to_numpy(dtype=float)
    d = pd.to_numeric(den, errors="coerce").to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(d > 0, n / d, np.nan)
    return out


def candidate_features(df: pd.DataFrame) -> pd.DataFrame:
    """The derived candidate columns (:data:`CAND_NUMERIC`), same row order as *df*."""
    ml = df["max_loss_mid"]
    out = pd.DataFrame(index=df.index)
    for c in (
        "dte",
        "anchor_delta",
        "width_pct",
        "static_pop",
        "managed_pop",
        "expected_days_held",
    ):
        out[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    for c in (
        "be_atr",
        "be_sigma",
        "credit_width",
        "debit_width",
        "rorc_day",
        "vrp",
        "ctx_liquidity",
    ):
        out[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    out["static_ev_per_risk"] = _ratio(df["static_net_ev"], ml)
    out["managed_ev_per_risk"] = _ratio(df["managed_net_ev"], ml)
    out["ev_proxy_per_risk"] = _ratio(df["ev_proxy"], ml)
    out["net_per_risk"] = _ratio(pd.to_numeric(df["net_mid"], errors="coerce") * 100.0, ml)
    out["gain_per_risk"] = _ratio(df["max_gain_mid"], ml)
    out["cost_per_risk"] = _ratio(df["est_cost"], ml)
    out["cand_spread_over_premium"] = pd.to_numeric(
        df["spread_to_premium"], errors="coerce"
    ).astype(float)
    out["log_leg_oi_min"] = np.log1p(pd.to_numeric(df["leg_oi_min"], errors="coerce").clip(lower=0))
    out["log_leg_volume_min"] = np.log1p(
        pd.to_numeric(df["leg_volume_min"], errors="coerce").clip(lower=0)
    )
    out["log_max_loss"] = np.log1p(pd.to_numeric(ml, errors="coerce").clip(lower=0))
    for c in (
        "credit",
        "shorts_sellable",
        "ctx_passes_filters",
        "ctx_credit_only",
        "ctx_entry_spread_ok",
        "ctx_earnings_blackout",
        "ctx_regime_blocked",
        "ctx_etf",
    ):
        out[c] = df[c].astype("float64") if c in df else np.nan
    return out[list(CAND_NUMERIC)]


def encode_categories(df: pd.DataFrame, vocab: dict[str, list[str]]) -> pd.DataFrame:
    """Integer codes per :data:`CAND_CATEGORICAL` column from the stored *vocab*
    (unseen / missing -> -1, which LightGBM treats as missing)."""
    out = pd.DataFrame(index=df.index)
    for c in CAND_CATEGORICAL:
        cats = vocab.get(c, [])
        s = (
            df[c].astype("string").fillna("__none__")
            if c in df
            else pd.Series("__none__", index=df.index)
        )
        out[f"cat_{c}"] = pd.Categorical(s, categories=cats).codes.astype("float64")
        out.loc[out[f"cat_{c}"] < 0, f"cat_{c}"] = np.nan
    return out


def build_vocab(df: pd.DataFrame) -> dict[str, list[str]]:
    return {
        c: sorted(df[c].astype("string").fillna("__none__").unique().tolist())
        for c in CAND_CATEGORICAL
        if c in df
    }


def cat_names() -> list[str]:
    return [f"cat_{c}" for c in CAND_CATEGORICAL]


def load_panel_range(
    features_root: Path, start: dt.date, end: dt.date, schema: str, cols: Sequence[str]
) -> pd.DataFrame:
    """Feature-store rows for every stored session in [start, end] (*cols* + id)."""
    from arc.ml.features.store import date_path, version_dir
    from arc.utils.calendar import sessions_between

    vdir = version_dir(features_root, schema)
    frames = []
    want = ["date", "ticker", *cols]
    for d in sessions_between(start, end):
        p = date_path(vdir, d)
        if p.is_file():
            frames.append(pd.read_parquet(p, columns=want))
    if not frames:
        msg = f"no feature panels in {vdir} for {start}..{end}"
        raise FileNotFoundError(msg)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_family(
    ledger_root: Path,
    profile: str,
    years: Sequence[int],
    x: str,
    *,
    cap_per_day: int | None,
    split: str = "dev",
    holdout_reason: str | None = None,
    filter_rows: str | None = None,
    for_scoring: bool = False,
    tickers: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Ledger rows of *profile* for *years*, subsampled per day (bounded memory).

    Adds ``inv_keep`` (the subsample's inverse keep fraction). *filter_rows*
    ``"passes_filters"`` keeps only rows that pass the ranking filters (the replay
    population, scored in full). *for_scoring* keeps rows whose label is missing or
    crosses the holdout (their labels are never read; a replay still needs a score).
    """
    from arc.ml.ledger.build import iter_ledger

    cols = ledger_columns(x)
    parts = []
    for _p, _y, df in iter_ledger(
        ledger_root,
        split=split,
        profiles=[profile],
        years=list(years),
        columns=cols,
        allow_holdout=split != "dev",
        reason=holdout_reason,
    ):
        if tickers is not None:
            df = df[df["ticker"].isin(list(tickers))]
        if not for_scoring:
            if split == "dev":
                df = df[~df["label_crosses_holdout"].fillna(False).astype(bool)]
            df = df[df[label_cols(x)["r"]].notna()]
        if filter_rows == "passes_filters":
            df = df[df["ctx_passes_filters"].fillna(False).astype(bool)]
        if cap_per_day is None:
            df = df.copy()
            df["inv_keep"] = 1.0
            parts.append(df.drop(columns=["split"], errors="ignore"))
            continue
        u = v.row_uniform(df["date"], df["ticker"], df["key"])
        keep, inv = v.stratified_day_sample(df, cap_per_day, u)
        df = df[keep].copy()
        df["inv_keep"] = inv[keep]
        parts.append(df.drop(columns=["split"], errors="ignore"))
    if not parts:
        msg = f"no ledger rows for {profile} {list(years)} ({split})"
        raise FileNotFoundError(msg)
    out = pd.concat(parts, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.date
    out["label_end_date"] = pd.to_datetime(out["label_end_date"]).dt.date
    return out.sort_values(["date", "ticker", "key"], kind="mergesort").reset_index(drop=True)


def attach_features(
    rows: pd.DataFrame, panel: pd.DataFrame, panel_cols: Sequence[str]
) -> pd.DataFrame:
    """*rows* + panel columns (left join on date/ticker) + candidate features."""
    j = rows.merge(
        panel[["date", "ticker", *panel_cols]], on=["date", "ticker"], how="left", sort=False
    )
    cand = candidate_features(j)
    return pd.concat([j, cand.drop(columns=[c for c in cand.columns if c in j.columns])], axis=1)


def design(df: pd.DataFrame, features: Sequence[str], vocab: dict[str, list[str]]) -> pd.DataFrame:
    """The model matrix: numeric *features* (float32-safe) + encoded categories."""
    num = [f for f in features if not f.startswith("cat_")]
    x = df[num].apply(pd.to_numeric, errors="coerce").astype("float64")
    cats = encode_categories(df, vocab)
    return pd.concat([x, cats[[f for f in features if f.startswith("cat_")]]], axis=1)[
        list(features)
    ]
