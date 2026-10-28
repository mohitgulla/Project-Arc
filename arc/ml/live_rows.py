"""Live M2 design rows (E27.4, D126): one ledger-shaped row per live Quant menu item.

The ledger (:mod:`arc.ml.ledger.build`) writes one row per sim menu candidate with its
entry facts and ``ctx_*`` context; M2 trains on those columns through
:func:`arc.ml.m2.dataset.candidate_features` / :func:`~arc.ml.m2.dataset.design`.
:func:`menu_rows` builds the *same raw columns* from a live menu item, so the live
scorer reuses those two functions unchanged (no second copy of a feature formula,
train/serve parity). The panel features (E27.1) join on ``ticker`` from
:func:`latest_panel`.

Pure on its inputs: the pipeline gathers the facts (:class:`LiveCandidate`) and this
module never reads the broker, an LLM or a prompt. Its output is context only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Iterable, Mapping, Sequence

    from arc.ml.features.compute import FeatureInputs

#: Live scanner strategy -> the ledger's ``StrategyKind`` value (the M2 ``kind`` vocab).
STRATEGY_KIND: Mapping[str, str] = {
    "bull_put": "bull_put",
    "bear_call": "bear_call",
    "iron_condor": "iron_condor",
    "long_call": "long_call",
    "long_put": "long_put",
    "bull_call_debit": "bull_call",
    "bear_put_debit": "bear_put",
}
CREDIT_KINDS = frozenset({"bull_put", "bear_call", "iron_condor"})
DIRECTION: Mapping[str, str] = {
    "long_call": "bullish",
    "bull_call": "bullish",
    "bull_put": "bullish",
    "long_put": "bearish",
    "bear_put": "bearish",
    "bear_call": "bearish",
    "iron_condor": "neutral",
}
_TREND = {1.0: "bull", 0.0: "sideways", -1.0: "bear"}
_VOL = {-1.0: "low", 0.0: "mid", 1.0: "high"}


@dataclass(frozen=True)
class LiveLeg:
    symbol: str
    side: int  # +1 long / -1 short
    mid: float
    spread: float


@dataclass(frozen=True)
class LiveCandidate:
    """One live menu item's entry facts (gathered by the pipeline, no I/O here)."""

    ticker: str
    strategy: str  # ScanStrategy value
    dte: int
    expiration: dt.date
    spot: float
    legs: tuple[LiveLeg, ...]
    net_mid: float  # Structure.net_debit_credit (per share, analyze sign convention)
    max_loss: float  # $ per unit
    max_gain: float | None
    breakevens: tuple[float, ...]
    short_deltas: tuple[float, ...]
    long_deltas: tuple[float, ...]
    width: float | None  # widest wing, $ per share
    credit_width: float | None
    ev_proxy: float
    atm_iv: float | None
    leg_oi_min: float | None
    leg_volume_min: float | None
    sleeve: str | None = None
    static_pop: float | None = None
    static_net_ev: float | None = None
    managed_pop: float | None = None
    managed_net_ev: float | None = None
    expected_days_held: float | None = None
    rorc_day: float | None = None
    vrp: float | None = None
    est_cost: float | None = None  # managed costs total, $ per unit (ledger est_cost)
    be_atr: float | None = None
    passes_filters: bool | None = None
    entry_spread_ok: bool | None = None
    earnings_blackout: bool | None = None
    regime_blocked: bool | None = None
    filter_reason: str | None = None
    credit_only: bool = False
    etf: bool = False
    rank_live: int | None = None  # position on the live (managed_net_ev_full) menu
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return STRATEGY_KIND.get(self.strategy, self.strategy)

    @property
    def key(self) -> str:
        """``+OCC,-OCC`` (sorted legs): the candidate's id in ``ml_scores``."""
        from arc.ml.scores import legs_key

        return legs_key([(leg.symbol, leg.side) for leg in self.legs])


def _nan(v: float | None) -> float:
    return math.nan if v is None else float(v)


def anchor_delta(c: LiveCandidate) -> float:
    """The ledger's spec delta: the anchor leg's |Δ| (a short leg; the long for a single
    long option), to two decimals like the ``_d<NN>`` spec bucket."""
    ds = c.short_deltas or c.long_deltas
    return round(float(ds[0]), 2) if ds else math.nan


def be_sigma(c: LiveCandidate) -> float:
    """Nearest breakeven move in σ of the expiry's ATM IV (ledger ``entry_facts``)."""
    if not c.breakevens or not c.atm_iv or c.atm_iv <= 0 or c.dte <= 0 or c.spot <= 0:
        return math.nan
    move = min(abs(b - c.spot) for b in c.breakevens)
    return move / (c.spot * c.atm_iv * math.sqrt(c.dte / 365.0))


def candidate_row(c: LiveCandidate, panel_row: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ledger's raw columns for *c* (the ones :data:`LEDGER_FEATURE_COLS` lists)."""
    kind = c.kind
    credit = kind in CREDIT_KINDS
    spread_sum = sum(leg.spread for leg in c.legs)
    p = panel_row or {}
    trend = p.get("trend_v2")
    vol = p.get("vol_state_v2")
    debit_width = (
        c.width / c.net_mid
        if not credit and c.width and len(c.legs) == 2 and c.net_mid > 0  # noqa: PLR2004
        else None
    )
    return {
        "ticker": c.ticker,
        "key": c.key,
        "kind": kind,
        "sleeve": c.sleeve,
        "direction": DIRECTION.get(kind),
        "credit": credit,
        "dte": c.dte,
        "anchor_delta": anchor_delta(c),
        "width_pct": (c.width / c.spot) if c.width and c.spot > 0 and len(c.legs) > 1 else None,
        "static_pop": _nan(c.static_pop),
        "static_net_ev": _nan(c.static_net_ev),
        "managed_pop": _nan(c.managed_pop),
        "managed_net_ev": _nan(c.managed_net_ev),
        "ev_proxy": c.ev_proxy,
        "net_mid": c.net_mid,
        "max_loss_mid": c.max_loss,
        "max_gain_mid": c.max_gain,
        "est_cost": _nan(c.est_cost),
        "expected_days_held": _nan(c.expected_days_held),
        "be_atr": _nan(c.be_atr),
        "be_sigma": be_sigma(c),
        "spread_to_premium": spread_sum / abs(c.net_mid) if c.net_mid else math.nan,
        "leg_oi_min": _nan(c.leg_oi_min),
        "leg_volume_min": _nan(c.leg_volume_min),
        "shorts_sellable": True,  # the live scanner never sells a zero-bid contract
        "credit_width": c.credit_width,
        "debit_width": debit_width,
        "rorc_day": _nan(c.rorc_day),
        "vrp": _nan(c.vrp),
        "ctx_trend": _label(_TREND, trend),
        "ctx_vol": _label(_VOL, vol),
        "ctx_filter_reason": c.filter_reason,
        "ctx_passes_filters": c.passes_filters,
        "ctx_credit_only": c.credit_only,
        "ctx_entry_spread_ok": c.entry_spread_ok,
        "ctx_earnings_blackout": c.earnings_blackout,
        "ctx_regime_blocked": c.regime_blocked,
        "ctx_liquidity": _nan(p.get("opt_volume")) if p else math.nan,
        "ctx_etf": c.etf,
        "rank_live": c.rank_live,
    }


def _label(codes: Mapping[float, str], v: Any) -> str:
    """A panel code (+1/0/-1) as the ledger's label; NaN / missing -> ``unknown``."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "unknown"
    return codes.get(f, "unknown") if math.isfinite(f) else "unknown"


def menu_rows(
    cands: Sequence[LiveCandidate], panel: pd.DataFrame | None, panel_cols: Sequence[str]
) -> pd.DataFrame:
    """Design-ready rows through the training join (:func:`~arc.ml.m2.dataset.attach_features`).

    *panel* holds one row per ticker (its latest session before today); each candidate
    row is dated at its ticker's panel session so the left join matches. A ticker
    without one gets NaN panel features (missing to the boosters; ``missing_features``
    counts them per score).
    """
    from arc.ml.m2.dataset import attach_features

    by_t: dict[str, dict[str, Any]] = {}
    if panel is not None and len(panel):
        for r in panel.to_dict(orient="records"):
            by_t[str(r["ticker"])] = r
    df = pd.DataFrame.from_records([candidate_row(c, by_t.get(c.ticker)) for c in cands])
    if df.empty:
        return df
    df["date"] = [by_t.get(t, {}).get("date") for t in df["ticker"]]
    if panel is not None and len(panel):
        p = panel.reindex(columns=["date", "ticker", *panel_cols])
    else:
        p = pd.DataFrame(columns=["date", "ticker", *panel_cols])
    p = p.astype({c: "float64" for c in panel_cols})
    df["date"] = df["date"].astype(object)
    p["date"] = p["date"].astype(object)
    return attach_features(df, p, panel_cols)


# ---------------------------------------------------------------------------
# Panel features for the live names (E27.1 live adapter)
# ---------------------------------------------------------------------------


def live_panel_root(data_dir: Path) -> Path:
    """Where ``ml.refresh`` keeps the live panels (store layout, one parquet per session):
    ``<data>/ml/live/features/v<schema>/date=<d>.parquet``."""
    return Path(data_dir) / "ml" / "live" / "features"


def stored_panel(data_dir: Path, schema: str, day: dt.date, tickers: Iterable[str]) -> pd.DataFrame:
    """The newest stored live panel dated ≤ *day*, restricted to *tickers* (may be empty)."""
    from arc.ml.features.store import version_dir

    vdir = version_dir(live_panel_root(data_dir), schema)
    if not vdir.is_dir():
        return pd.DataFrame()
    files = sorted(
        p for p in vdir.glob("date=*.parquet") if p.stem.split("=", 1)[1] <= day.isoformat()
    )
    if not files:
        return pd.DataFrame()
    df = pd.read_parquet(files[-1])
    want = sorted({t.upper() for t in tickers})
    return pd.DataFrame(df[df["ticker"].astype(str).isin(want)]).reset_index(drop=True)


def latest_panel(inputs: FeatureInputs, day: dt.date, tickers: Iterable[str]) -> pd.DataFrame:
    """One panel row per ticker: its latest session ≤ *day* with a close (point in time)."""
    from arc.ml.features.compute import compute_panel

    sessions = [s for s in inputs.sessions if s <= day][-5:]
    if not sessions:
        return pd.DataFrame()
    p = compute_panel(inputs, sessions, list(tickers))
    if p.empty:
        return p
    p = p.sort_values(["ticker", "date"]).groupby("ticker", as_index=False).tail(1)
    return p.reset_index(drop=True)
