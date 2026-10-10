"""Daily market-health read: each market gauge against its own history (E16.4, D76).

Pure: no network, no LLM, no clock. The ``market_health`` routine
(:func:`arc.routines.handlers.market_health_source`) gathers the inputs (Cboe
VIX / VVIX history, the ``pc_history`` series built from ``options_daily``, the
``vol_term`` entry and the active list's ``regime.technicals``) and calls
:func:`compute_market_health`.

Definitions (fractions throughout: ``0.22`` = 22 %, a percentile of ``0.22`` is p22):

* ``vix_sma50``: mean of the last 50 VIX closes on or before ``as_of`` (today included).
* ``vix_vs_sma50_pct``: ``vix / vix_sma50 - 1``.
* ``*_pct_1y``: share of the previous :data:`PCT_LOOKBACK` observations (strictly
  before the current one) that are strictly below the current value; the same rule
  as :func:`arc.features.vol.iv_percentile`. Needs :data:`MIN_PCT_OBS` prior
  observations, else ``None`` (listed in ``missing``).
* ``pc_*_5d``: mean of the last 5 session ratios; its percentile ranks today's 5-day
  mean against the previous 5-day means (each over its own 5 sessions).
* Breadth: share of the active names with fresh technicals whose close is above
  SMA50 / SMA200 (names without that SMA are left out of that share).

Walk-forward safe: every series is cut at ``as_of`` before anything is computed, so a
value never depends on later data. A stale input (its last observation more than
``max_lag_sessions`` sessions before ``as_of``) makes its fields ``None`` and names
it in ``missing``; nothing is carried forward. Context only: never a gate input.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic fields resolve it at runtime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

__all__ = [
    "MIN_PCT_OBS",
    "PCT_LOOKBACK",
    "SMA_WINDOW",
    "AVG_WINDOW",
    "Breadth",
    "HealthThresholds",
    "IndexClose",
    "IndexHistoryPayload",
    "MarketHealthPayload",
    "MarketTerm",
    "PcHistoryPayload",
    "PcPoint",
    "breadth_from_technicals",
    "compute_market_health",
    "health_labels",
    "market_health_line",
    "merge_pc_points",
    "percentile_prior",
    "rolling_mean",
]

#: Prior observations a 1-year percentile looks back over (one trading year).
PCT_LOOKBACK = 252
#: Fewer prior observations than this: the percentile is ``None``.
MIN_PCT_OBS = 120
SMA_WINDOW = 50
AVG_WINDOW = 5
#: Trailing sessions kept on a history entry (a year of percentiles + the 5-day mean).
HISTORY_KEEP = 300

_FORBID = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Context payloads (registered in arc.context.kinds)
# ---------------------------------------------------------------------------


class IndexClose(BaseModel):
    model_config = _FORBID

    day: dt.date
    close: float = Field(..., gt=0)


class IndexHistoryPayload(BaseModel):
    """Trailing daily closes of one Cboe index (``VIX`` / ``VVIX``); subject = index."""

    model_config = _FORBID

    index: str
    as_of: dt.date = Field(..., description="Date of the last close")
    closes: list[IndexClose] = Field(..., min_length=1, description="Oldest first")
    source: Literal["cboe"] = "cboe"
    url: str


class PcPoint(BaseModel):
    """One session's Cboe put/call ratios (equity and total)."""

    model_config = _FORBID

    day: dt.date
    equity: float | None = Field(None, ge=0)
    total: float | None = Field(None, ge=0)


class PcHistoryPayload(BaseModel):
    """Daily Cboe put/call history (from ``options_daily`` + a one-off backfill of
    Cboe's dated daily statistics); subject = ``market``."""

    model_config = _FORBID

    as_of: dt.date = Field(..., description="Date of the last point")
    points: list[PcPoint] = Field(..., min_length=1, description="Oldest first")
    source: Literal["cboe"] = "cboe"


class MarketTerm(BaseModel):
    """VIX term structure copied from the fresh ``vol_term`` entry."""

    model_config = _FORBID

    structure: Literal["contango", "flat", "backwardation"]
    ratio_9d_1m: float | None = None
    ratio_3m_1m: float | None = None


class MarketHealthPayload(BaseModel):
    """E16.4 (D76): the daily market-health read; subject = ``market``."""

    model_config = ConfigDict(extra="forbid")

    as_of: dt.date = Field(..., description="Session the read describes")
    vix: float | None = None
    vix_sma50: float | None = None
    vix_vs_sma50_pct: float | None = Field(None, description="vix / vix_sma50 - 1")
    vix_pct_1y: float | None = Field(None, ge=0.0, le=1.0)
    vvix: float | None = None
    vvix_pct_1y: float | None = Field(None, ge=0.0, le=1.0)
    term: MarketTerm | None = None
    pc_equity: float | None = None
    pc_equity_5d: float | None = None
    pc_equity_5d_pct_1y: float | None = Field(None, ge=0.0, le=1.0)
    pc_total_5d: float | None = None
    pc_total_5d_pct_1y: float | None = Field(None, ge=0.0, le=1.0)
    breadth_above_50d: float | None = Field(None, ge=0.0, le=1.0)
    breadth_above_200d: float | None = Field(None, ge=0.0, le=1.0)
    breadth_n: int = Field(0, ge=0, description="Active names with fresh technicals")
    squeeze_count: int | None = Field(None, ge=0)
    labels: list[str] = Field(default_factory=list)
    sources_as_of: dict[str, str | None] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Math (pure)
# ---------------------------------------------------------------------------


def percentile_prior(
    values: Sequence[float], *, lookback: int = PCT_LOOKBACK, min_obs: int = MIN_PCT_OBS
) -> float | None:
    """Share of the up-to-*lookback* values before the last one that are strictly
    below it (0..1); ``None`` with fewer than *min_obs* prior values."""
    if not values:
        return None
    cur = values[-1]
    prior = list(values[:-1])[-lookback:]
    if len(prior) < min_obs:
        return None
    return sum(1 for v in prior if v < cur) / len(prior)


def rolling_mean(values: Sequence[float], window: int) -> list[float]:
    """Trailing means of *window* values (``len(values) - window + 1`` of them)."""
    if window < 1:
        msg = "window must be >= 1"
        raise ValueError(msg)
    out: list[float] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= window:
            total -= values[i - window]
        if i >= window - 1:
            out.append(total / window)
    return out


def merge_pc_points(*groups: Iterable[PcPoint], keep: int = HISTORY_KEEP) -> list[PcPoint]:
    """Union of put/call points by day (a later group wins a day), oldest first,
    trimmed to the last *keep* days."""
    by_day: dict[dt.date, PcPoint] = {}
    for group in groups:
        for p in group:
            by_day[p.day] = p
    return [by_day[d] for d in sorted(by_day)][-keep:]


class Breadth(BaseModel):
    model_config = _FORBID

    above_50d: float | None = None
    above_200d: float | None = None
    n: int = 0
    squeeze_count: int | None = None


def breadth_from_technicals(techs: Iterable[Mapping[str, object]]) -> Breadth:
    """Breadth from stored ``TechnicalFeatures`` dumps (one per active name)."""

    def num(t: Mapping[str, object], key: str) -> float | None:
        v = t.get(key)
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

    rows = [t for t in techs if num(t, "close") is not None]
    if not rows:
        return Breadth()

    def share(key: str) -> float | None:
        flags = [
            num(t, "close") > sma  # type: ignore[operator]
            for t in rows
            if (sma := num(t, key)) is not None
        ]
        return sum(flags) / len(flags) if flags else None

    return Breadth(
        above_50d=share("sma50"),
        above_200d=share("sma200"),
        n=len(rows),
        squeeze_count=sum(1 for t in rows if t.get("squeeze_on") is True),
    )


class HealthThresholds(BaseModel):
    """Label thresholds (``market_health:`` in routines.yaml; labels only)."""

    model_config = _FORBID

    vix_stretched_ratio: Annotated[float, Field(gt=1.0, le=3.0)] = 1.2
    vix_stretched_pct: Annotated[float, Field(ge=0.5, le=1.0)] = 0.8
    vix_compressed_pct: Annotated[float, Field(ge=0.0, le=0.5)] = 0.2
    pc_fear_pct: Annotated[float, Field(ge=0.5, le=1.0)] = 0.9
    pc_greed_pct: Annotated[float, Field(ge=0.0, le=0.5)] = 0.1
    breadth_weak: Annotated[float, Field(ge=0.0, le=1.0)] = 0.4
    breadth_strong: Annotated[float, Field(ge=0.0, le=1.0)] = 0.7
    max_lag_sessions: Annotated[int, Field(ge=0, le=10)] = 2


def health_labels(p: MarketHealthPayload, th: HealthThresholds) -> list[str]:
    """Deterministic labels (fixed order). ``pc_*`` read the equity 5-day percentile,
    ``breadth_*`` the share above SMA50."""
    out: list[str] = []
    stretched = (
        p.vix is not None
        and p.vix_sma50 is not None
        and p.vix >= th.vix_stretched_ratio * p.vix_sma50
    ) or (p.vix_pct_1y is not None and p.vix_pct_1y >= th.vix_stretched_pct)
    if stretched:
        out.append("vix_stretched_high")
    if p.vix_pct_1y is not None and p.vix_pct_1y <= th.vix_compressed_pct:
        out.append("vix_compressed")
    pc = p.pc_equity_5d_pct_1y
    if pc is not None and pc >= th.pc_fear_pct:
        out.append("pc_extreme_fear")
    if pc is not None and pc <= th.pc_greed_pct:
        out.append("pc_extreme_greed")
    b = p.breadth_above_50d
    if b is not None and b < th.breadth_weak:
        out.append("breadth_weak")
    if b is not None and b > th.breadth_strong:
        out.append("breadth_strong")
    return out


def _cut(series: Iterable[tuple[dt.date, float]], as_of: dt.date) -> list[tuple[dt.date, float]]:
    return sorted((d, v) for d, v in series if d <= as_of)


def compute_market_health(
    *,
    as_of: dt.date,
    vix: Iterable[tuple[dt.date, float]] = (),
    vvix: Iterable[tuple[dt.date, float]] = (),
    pc: Iterable[PcPoint] = (),
    term: tuple[dt.date, MarketTerm] | None = None,
    technicals: Iterable[Mapping[str, object]] = (),
    technicals_as_of: dt.date | None = None,
    lag: Callable[[dt.date, dt.date], int],
    thresholds: HealthThresholds | None = None,
) -> MarketHealthPayload:
    """The read for session *as_of*.

    *lag(day, as_of)* counts trading sessions in ``(day, as_of]`` (the caller passes a
    calendar-backed function, which keeps this module pure). An input whose last
    observation lags more than ``max_lag_sessions`` is stale. *technicals* must already
    be the fresh entries (the caller filters by date); *technicals_as_of* is their
    newest date, for ``sources_as_of``.
    """
    th = thresholds or HealthThresholds()
    missing: list[str] = []
    src: dict[str, str | None] = {}

    def fresh(name: str, day: dt.date | None) -> bool:
        src[name] = day.isoformat() if day else None
        if day is None:
            missing.append(f"{name}: no data")
            return False
        if lag(day, as_of) > th.max_lag_sessions:
            missing.append(f"{name}: stale ({day.isoformat()})")
            return False
        return True

    fields: dict[str, object] = {}

    vix_s = _cut(vix, as_of)
    if fresh("vix", vix_s[-1][0] if vix_s else None):
        vals = [v for _, v in vix_s]
        fields["vix"] = vals[-1]
        if len(vals) >= SMA_WINDOW:
            sma = sum(vals[-SMA_WINDOW:]) / SMA_WINDOW
            fields["vix_sma50"] = round(sma, 4)
            fields["vix_vs_sma50_pct"] = round(vals[-1] / sma - 1.0, 4)
        else:
            missing.append(f"vix_sma50: {len(vals)} closes < {SMA_WINDOW}")
        fields["vix_pct_1y"] = _pct(vals, "vix_pct_1y", missing)

    vvix_s = _cut(vvix, as_of)
    if fresh("vvix", vvix_s[-1][0] if vvix_s else None):
        vals = [v for _, v in vvix_s]
        fields["vvix"] = vals[-1]
        fields["vvix_pct_1y"] = _pct(vals, "vvix_pct_1y", missing)

    if term is not None and term[0] > as_of:
        term = None
    if fresh("vol_term", term[0] if term else None) and term is not None:
        fields["term"] = term[1]

    pts = sorted((p for p in pc if p.day <= as_of), key=lambda p: p.day)
    if fresh("options_daily", pts[-1].day if pts else None):
        for seg in ("equity", "total"):
            vals = [float(v) for p in pts if (v := getattr(p, seg)) is not None]
            if seg == "equity" and pts[-1].equity is not None:
                fields["pc_equity"] = pts[-1].equity
            if len(vals) < AVG_WINDOW:
                missing.append(f"pc_{seg}_5d: {len(vals)} sessions < {AVG_WINDOW}")
                continue
            means = rolling_mean(vals, AVG_WINDOW)
            fields[f"pc_{seg}_5d"] = round(means[-1], 4)
            fields[f"pc_{seg}_5d_pct_1y"] = _pct(means, f"pc_{seg}_5d_pct_1y", missing)

    techs = list(technicals)
    src["technicals"] = technicals_as_of.isoformat() if technicals_as_of else None
    b = breadth_from_technicals(techs)
    if b.n == 0:
        missing.append("breadth: no fresh technicals on the active list")
    fields.update(
        breadth_above_50d=None if b.above_50d is None else round(b.above_50d, 4),
        breadth_above_200d=None if b.above_200d is None else round(b.above_200d, 4),
        breadth_n=b.n,
        squeeze_count=b.squeeze_count,
    )

    payload = MarketHealthPayload(as_of=as_of, sources_as_of=src, missing=missing, **fields)  # type: ignore[arg-type]
    payload.labels = health_labels(payload, th)
    return payload


def _pct(values: Sequence[float], name: str, missing: list[str]) -> float | None:
    pct = percentile_prior(values)
    if pct is None:
        missing.append(f"{name}: {max(0, len(values) - 1)} prior obs < {MIN_PCT_OBS}")
        return None
    return round(pct, 4)


# ---------------------------------------------------------------------------
# Research line (code-rendered; shown only with personas.market_health_context on)
# ---------------------------------------------------------------------------

_VIX_WORD = {"vix_stretched_high": "stretched high", "vix_compressed": "compressed"}
_PC_WORD = {"pc_extreme_fear": "fear", "pc_extreme_greed": "greed"}
_BREADTH_WORD = {"breadth_weak": "weak", "breadth_strong": "strong"}


def _p(x: float) -> str:
    return f"p{round(x * 100):.0f}"


def market_health_line(p: Mapping[str, object]) -> str:
    """``Market health (10-09): VIX 15.4 (-8% vs 50d, 1y p22) · VVIX 88 (p40) · P/C eq
    5d 0.61 (p15, greed) · breadth 62% >50d / 71% >200d · 4 in squeeze`` from a stored
    payload dump. A part whose inputs are missing is dropped."""
    m = MarketHealthPayload.model_validate(dict(p))
    labels = set(m.labels)

    def words(table: Mapping[str, str]) -> list[str]:
        return [w for k, w in table.items() if k in labels]

    parts: list[str] = []
    if m.vix is not None:
        inner = []
        if m.vix_vs_sma50_pct is not None:
            inner.append(f"{round(m.vix_vs_sma50_pct, 2) + 0.0:+.0%} vs 50d")
        if m.vix_pct_1y is not None:
            inner.append(f"1y {_p(m.vix_pct_1y)}")
        inner += words(_VIX_WORD)
        parts.append(f"VIX {m.vix:.1f}" + (f" ({', '.join(inner)})" if inner else ""))
    if m.vvix is not None:
        inner = [_p(m.vvix_pct_1y)] if m.vvix_pct_1y is not None else []
        parts.append(f"VVIX {m.vvix:.0f}" + (f" ({', '.join(inner)})" if inner else ""))
    if m.pc_equity_5d is not None:
        inner = [_p(m.pc_equity_5d_pct_1y)] if m.pc_equity_5d_pct_1y is not None else []
        inner += words(_PC_WORD)
        parts.append(
            f"P/C eq 5d {m.pc_equity_5d:.2f}" + (f" ({', '.join(inner)})" if inner else "")
        )
    shares = []
    if m.breadth_above_50d is not None:
        shares.append(f"{m.breadth_above_50d:.0%} >50d")
    if m.breadth_above_200d is not None:
        shares.append(f"{m.breadth_above_200d:.0%} >200d")
    if shares:
        bw = words(_BREADTH_WORD)
        parts.append(
            f"breadth {' / '.join(shares)} (n {m.breadth_n}" + (f", {bw[0]}" if bw else "") + ")"
        )
    if m.squeeze_count is not None and m.breadth_n:
        parts.append(f"{m.squeeze_count} in squeeze")
    head = f"Market health ({m.as_of:%m-%d}):"
    return f"{head} {' · '.join(parts)}" if parts else f"{head} no fresh inputs"
