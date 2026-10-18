# ruff: noqa: E501 - FEATURES is a table, one spec per line
"""The feature registry of the ML feature store (E27.1, D126).

One :class:`FeatureSpec` per feature. :data:`FEATURE_SCHEMA_VERSION` hashes the whole
registry **and** the source of :mod:`arc.ml.features.compute`, so any edit to a spec or
to a formula changes it; datasets and models record it, and a scorer refuses a model
whose version differs from the running one (E27.4).

``available_live=False`` marks a feature the live store cannot produce yet (no stored
input). It stays in the research panel but :func:`default_model_features` drops it, so a
live model never trains on a column it would see as all-null in production.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Group = Literal[
    "ticker_price",
    "ticker_options",
    "market",
    "calendar",
    "event",
    "portfolio",
    "candidate",
    "model",
]
Dtype = Literal["float", "int", "bool", "category"]
NullPolicy = Literal["keep_nan", "missing_indicator", "zero", "category_unknown"]
Asof = Literal["close", "prev_close"]

#: Groups :func:`arc.ml.features.compute.compute_panel` produces (portfolio and
#: candidate rows are supplied by the caller: the sim / live book and the menu row).
PANEL_GROUPS: tuple[str, ...] = ("ticker_price", "ticker_options", "market", "calendar", "event")


class FeatureSpec(BaseModel):
    """One registered feature (the data contract of a model input column)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    group: Group
    dtype: Dtype = "float"
    window: int = Field(0, ge=0, description="sessions read (0 = this session only)")
    source_tables: tuple[str, ...]
    available_live: bool = True
    asof: Asof = "close"
    description: str
    null_policy: NullPolicy = "keep_nan"
    version: int = Field(1, ge=1)


_OHLC = ("underlying_ohlc", "market_tape:history_bars")
_OHLC_SPY = (*_OHLC, "underlying_ohlc:SPY")
_OPT_IV = ("option_stats/daily", "iv_daily")
_OPT_ONLY = ("option_stats/daily",)
_OPT_VOL = ("option_stats/daily", "context_entries:chain_snapshot")
_OPT_SNAP = ("option_stats/daily", "context_entries:chain_snapshot")
_UNIV = ("underlying_ohlc", "backtest_universe/membership")
_MACRO = ("config/backtest/macro_calendar_2020_2026.yaml",)
_EARN = ("option_stats/earnings_events.parquet", "raw_docs:earnings")
_BOOK = ("sim book", "open_structures", "pnl_snapshots")
_MENU = ("menu row (arc.pipeline scanner)",)


def _f(
    name: str, group: Group, window: int, src: tuple[str, ...], desc: str, **kw: Any
) -> FeatureSpec:
    return FeatureSpec(
        name=name, group=group, window=window, source_tables=src, description=desc, **kw
    )


_P: Group = "ticker_price"
_O: Group = "ticker_options"
_M: Group = "market"
_C: Group = "calendar"
_E: Group = "event"
_PF: Group = "portfolio"
_CD: Group = "candidate"

FEATURES: tuple[FeatureSpec, ...] = (
    # --- ticker price -------------------------------------------------------------
    _f("ret_1d", _P, 1, _OHLC, "close / close 1 session ago - 1"),
    _f("ret_5d", _P, 5, _OHLC, "5-session simple return"),
    _f("ret_20d", _P, 20, _OHLC, "20-session simple return"),
    _f("ret_60d", _P, 60, _OHLC, "60-session simple return"),
    _f("ret_252_21", _P, 252, _OHLC, "12-1 momentum: close[t-21] / close[t-252] - 1"),
    _f("dist_ma20_atr", _P, 20, _OHLC, "(close - MA20) / ATR14 (mean true range)"),
    _f("dist_ma50_atr", _P, 50, _OHLC, "(close - MA50) / ATR14"),
    _f("dist_ma200_atr", _P, 200, _OHLC, "(close - MA200) / ATR14"),
    _f("dd_252", _P, 252, _OHLC, "close / rolling 252-session max close - 1"),
    _f("atr14_pct", _P, 15, _OHLC, "ATR14 (14-session mean true range) / close"),
    _f("gap_1d", _P, 1, _OHLC, "open / previous close - 1 (today's gap)"),
    _f("rv5", _P, 5, _OHLC, "annualised close-to-close vol, 5 log returns"),
    _f("rv20", _P, 20, _OHLC, "annualised close-to-close vol, 20 log returns"),
    _f("rv60", _P, 60, _OHLC, "annualised close-to-close vol, 60 log returns"),
    _f("pk5", _P, 5, _OHLC, "annualised Parkinson high/low vol, 5 sessions"),
    _f("pk20", _P, 20, _OHLC, "annualised Parkinson high/low vol, 20 sessions"),
    _f("pk60", _P, 60, _OHLC, "annualised Parkinson high/low vol, 60 sessions"),
    _f("trend_z_v2", _P, 60, _OHLC, "v2 vol-scaled 20-session trend z (arc.features.regime)"),
    _f("trend_v2", _P, 60, _OHLC, "v2 trend label: +1 bull / 0 sideways / -1 bear"),
    _f("vol_pct_v2", _P, 272, _OHLC, "v2 vol read: rv20 percentile in its trailing 252 values"),
    _f("vol_state_v2", _P, 272, _OHLC, "v2 vol state: -1 low / 0 mid / +1 high"),
    _f("beta_60", _P, 60, _OHLC_SPY, "60-session log-return beta to SPY"),
    _f("corr_60", _P, 60, _OHLC_SPY, "60-session log-return correlation to SPY"),
    _f("idio_vol_60", _P, 60, _OHLC_SPY, "annualised residual vol after the SPY beta, 60 sessions"),
    # --- ticker options -------------------------------------------------------------
    _f("iv30", _O, 0, _OPT_IV, "30-day constant-maturity ATM IV (live: iv_daily series)"),
    _f("iv60", _O, 0, _OPT_ONLY, "60-day constant-maturity ATM IV", available_live=False),
    _f("front_iv", _O, 0, _OPT_ONLY, "ATM IV of the front expiry in the chain window", available_live=False),
    _f("iv_rank", _O, 252, _OPT_IV, "IV30 rank over its last 252 observations (min 120; D55)"),
    _f("iv_pct", _O, 252, _OPT_IV, "IV30 percentile over its last 252 observations (min 120)"),
    _f("term_slope", _O, 0, _OPT_ONLY, "IV60 - IV30", available_live=False),
    _f("front_over_iv30", _O, 0, _OPT_ONLY, "front IV / IV30", available_live=False),
    _f("skew_25d", _O, 0, _OPT_ONLY, "30-day 25-delta put IV - call IV", available_live=False),
    _f("put_wing_25d", _O, 0, _OPT_ONLY, "30-day 25-delta put IV - IV30", available_live=False),
    _f("call_wing_25d", _O, 0, _OPT_ONLY, "30-day 25-delta call IV - IV30", available_live=False),
    _f("vrp20", _O, 20, (*_OPT_IV, *_OHLC), "IV30 - rv20"),
    _f("iv30_over_har", _O, 273, (*_OPT_IV, *_OHLC), "IV30 / HAR-RV 21-session vol forecast (252-row fit)"),
    _f("atm_spread_pct", _O, 0, _OPT_SNAP, "median ATM quoted spread / mid (live: chain snapshot ATM book)"),
    _f("two_sided_pct", _O, 0, _OPT_ONLY, "share of chain rows with a two-sided quote", available_live=False),
    _f("oi_missing", _O, 0, _OPT_ONLY, "1 when the day has no open interest (free tier); OI columns are then null", dtype="bool", available_live=False),
    _f("oi_total", _O, 0, _OPT_ONLY, "chain open interest", null_policy="missing_indicator", available_live=False),
    _f("oi_near_atm", _O, 0, _OPT_ONLY, "near-ATM OI (E7.9 step 5b band)", null_policy="missing_indicator", available_live=False),
    _f("put_call_oi", _O, 0, _OPT_ONLY, "put OI / call OI", null_policy="missing_indicator", available_live=False),
    _f("opt_volume", _O, 0, _OPT_VOL, "session option volume, whole chain (live: last Cboe snapshot of the session)"),
    _f("put_call_vol", _O, 0, _OPT_VOL, "put volume / call volume"),
    _f("opt_vol_ratio_20", _O, 20, _OPT_VOL, "option volume / its 20-session mean"),
    # --- market ---------------------------------------------------------------------
    _f("spy_trend_z", _M, 60, _OHLC_SPY, "SPY v2 trend z"),
    _f("spy_trend", _M, 60, _OHLC_SPY, "SPY v2 trend label (+1 / 0 / -1)"),
    _f("spy_dist_ma50", _M, 50, _OHLC_SPY, "SPY close / MA50 - 1"),
    _f("spy_dist_ma200", _M, 200, _OHLC_SPY, "SPY close / MA200 - 1"),
    _f("spy_dd_252", _M, 252, _OHLC_SPY, "SPY drawdown from its 252-session high"),
    _f("vix_proxy", _M, 0, _OPT_IV, "SPY IV30 (VIX proxy)"),
    _f("vix_proxy_pct_252", _M, 252, _OPT_IV, "SPY IV30 percentile in its last 252 sessions (min 120 obs)"),
    _f("spy_term_slope", _M, 0, _OPT_ONLY, "SPY IV60 - IV30", available_live=False),
    _f("breadth_ma50", _M, 50, _UNIV, "share of the PIT universe above its MA50", available_live=False),
    _f("dispersion_20d", _M, 20, _UNIV, "cross-sectional stdev of PIT-universe 20-session returns", available_live=False),
    _f("avg_corr_60", _M, 60, _UNIV, "mean pairwise 60-session return correlation of the PIT universe", available_live=False),
    _f("spy_flip_age", _M, 180, _OHLC_SPY, "sessions since the SPY v2 label last changed (capped 120)"),
    # --- calendar -------------------------------------------------------------------
    _f("sess_to_fomc", _C, 0, _MACRO, "sessions to the next FOMC decision day (0 = today)"),
    _f("sess_to_cpi", _C, 0, _MACRO, "sessions to the next CPI release"),
    _f("sess_to_nfp", _C, 0, _MACRO, "sessions to the next NFP release"),
    _f("dow", _C, 0, (), "day of week, 0 = Monday", dtype="int"),
    _f("month_end_3s", _C, 0, (), "1 within the last 3 sessions of the month", dtype="bool"),
    _f("sess_to_opex", _C, 0, (), "sessions to the next monthly OPEX (3rd Friday)"),
    _f("opex_week", _C, 0, (), "1 in the ISO week of the monthly OPEX", dtype="bool"),
    # --- event (earnings) -------------------------------------------------------------
    _f("sess_to_earn", _E, 0, _EARN, "sessions to the next earnings reaction session (see earn_src)"),
    _f("earn_src", _E, 0, _EARN, "0 none / 1 projected from the last detected event (+91d) / 2 calendar", dtype="category", null_policy="category_unknown"),
    _f("sess_since_earn", _E, 0, _EARN, "sessions since the last detected reaction session"),
    _f("last_earn_move_ratio", _E, 0, _EARN, "last event's realised move / implied move"),
    # --- portfolio (caller-supplied) ---------------------------------------------------
    _f("open_positions", _PF, 0, _BOOK, "open structures"),
    _f("open_risk_pct", _PF, 0, _BOOK, "sum of open max loss / equity"),
    _f("open_same_dir_share", _PF, 0, _BOOK, "share of open structures in the candidate's direction"),
    _f("same_name_open", _PF, 0, _BOOK, "open structures on the same underlying"),
    _f("same_cluster_open", _PF, 60, _BOOK, "open names with 60-session return corr > 0.7"),
    _f("same_sector_open", _PF, 0, _BOOK, "open names in the same sector group"),
    _f("drawdown_from_peak", _PF, 0, _BOOK, "account equity / peak equity - 1"),
    # --- candidate (computed by the scanner/menu code) -----------------------------
    _f("cand_kind", _CD, 0, _MENU, "structure kind", dtype="category", null_policy="category_unknown"),
    _f("cand_direction", _CD, 0, _MENU, "bull / bear / neutral", dtype="category", null_policy="category_unknown"),
    _f("cand_sleeve", _CD, 0, _MENU, "DTE sleeve (config/account_profiles.yaml)", dtype="category", null_policy="category_unknown"),
    _f("cand_dte", _CD, 0, _MENU, "calendar days to expiration", dtype="int"),
    _f("cand_anchor_delta", _CD, 0, _MENU, "anchor leg |delta|"),
    _f("cand_short_delta", _CD, 0, _MENU, "short leg |delta| (NaN for single legs)"),
    _f("cand_long_delta", _CD, 0, _MENU, "long leg |delta|"),
    _f("cand_width_pct_spot", _CD, 0, _MENU, "strike width / spot"),
    _f("cand_net_over_width", _CD, 0, _MENU, "credit or debit / width"),
    _f("cand_be_dist_sigma", _CD, 0, _MENU, "breakeven distance in sigma*sqrt(t) (IV30)"),
    _f("cand_be_dist_atr", _CD, 0, _MENU, "breakeven distance in ATR14"),
    _f("cand_spread_over_premium", _CD, 0, _MENU, "quoted spread / premium"),
    _f("cand_min_leg_oi", _CD, 0, _MENU, "min open interest over legs"),
    _f("cand_min_leg_volume", _CD, 0, _MENU, "min volume over legs"),
    _f("cand_ev", _CD, 0, _MENU, "managed EV (a feature, not a truth)"),
    _f("cand_pop", _CD, 0, _MENU, "managed PoP"),
    _f("cand_rorc_day", _CD, 0, _MENU, "return on risk capital per day"),
    _f("cand_vrp", _CD, 0, _MENU, "the menu row's VRP"),
)  # fmt: skip

# --- M1 baselines (E27.2): panel features, kept as model inputs -----------------------
_M1_H = (5, 10, 21, 42)
_M1_Q = ("05", "10", "25", "50", "75", "90", "95")
_MOD: Group = "model"
_M1_SRC = ("data/ml/models/m1/<version> (arc.ml.m1.predict)",)

FEATURES = (
    *FEATURES,
    *(
        _f(f"har_rv_h{h}", _P, 252 + h + 21, _OHLC, f"HAR-RV forecast of the next {h} sessions' annualised vol (252-row fit)")
        for h in _M1_H
    ),
    *(
        _f(f"hq_q{q}_h{h}", _P, 252 + h + 20, _OHLC, f"historical-quantile baseline: q{q} of the {h}-session log return (trailing 252 rv20-standardised returns x HAR vol)")
        for h in _M1_H
        for q in _M1_Q
    ),
    _f("history_short", _P, 252, _OHLC, "1 when the ticker has no close 252 sessions back", dtype="bool"),
    # --- M1 outputs (E27.2): produced by arc.ml.m1.predict, never by compute_panel ------
    *(
        _f(f"m1_q{q}_h{h}", _MOD, 0, _M1_SRC, f"M1 conformal q{q} of the {h}-session log return")
        for h in _M1_H
        for q in _M1_Q
    ),
    *(_f(f"m1_rv_h{h}", _MOD, 0, _M1_SRC, f"M1 median realised vol over the next {h} sessions (annualised)") for h in _M1_H),
    *(_f(f"m1_p_up_h{h}", _MOD, 0, _M1_SRC, f"M1 P(log return over {h} sessions > 0)") for h in _M1_H),
    *(_f(f"m1_move_vs_implied_h{h}", _MOD, 0, _M1_SRC, f"M1 E|{h}-session log return| / (IV30 x sqrt({h}/252))") for h in _M1_H),
    _f("m1_direction", _MOD, 0, _M1_SRC, "+1 / -1 = sign of m1_q50_h21 when |median| > 0.25 x (q75 - q25), else 0", dtype="int"),
)  # fmt: skip


def registry() -> dict[str, FeatureSpec]:
    out: dict[str, FeatureSpec] = {}
    for f in FEATURES:
        if f.name in out:
            msg = f"duplicate feature {f.name}"
            raise ValueError(msg)
        out[f.name] = f
    return out


def panel_feature_names() -> list[str]:
    """Features :func:`~arc.ml.features.compute.compute_panel` produces, registry order."""
    return [f.name for f in FEATURES if f.group in PANEL_GROUPS]


def default_model_features(specs: tuple[FeatureSpec, ...] = FEATURES) -> list[str]:
    """The default model feature list: every ``available_live`` feature (research may add more)."""
    return [f.name for f in specs if f.available_live]


def schema_version(
    specs: tuple[FeatureSpec, ...] = FEATURES, compute_source: bytes | None = None
) -> str:
    """12-hex sha256 of the registry JSON and the bytes of ``compute.py``."""
    if compute_source is None:
        compute_source = (Path(__file__).parent / "compute.py").read_bytes()
    h = hashlib.sha256()
    body = [f.model_dump(mode="json") for f in specs]
    h.update(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    h.update(b"\0")
    h.update(compute_source)
    return h.hexdigest()[:12]


@cache
def _running_version() -> str:
    return schema_version()


def __getattr__(name: str) -> str:  # lazy so importing the registry stays cheap
    if name == "FEATURE_SCHEMA_VERSION":
        return _running_version()
    raise AttributeError(name)


class SchemaMismatchError(RuntimeError):
    """A dataset or model built with a different feature schema than the running one."""


def require_schema(version: str) -> None:
    """Raise :class:`SchemaMismatchError` unless *version* is the running schema."""
    running = _running_version()
    if version != running:
        msg = f"feature schema {version} != running {running}; rebuild features / retrain"
        raise SchemaMismatchError(msg)
