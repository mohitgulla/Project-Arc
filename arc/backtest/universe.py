"""E7.7 (D84): the point-in-time backtest universe — exactly 500 names, C125 ⊂ C250 ⊂ C500.

The backtest must not be limited to today's winners. For every quarter start ``Q``
(2020-01-01 on) this module ranks the optionable names using only data a trader had
before ``Q``, then folds the quarters into one priority-ordered **pull list** of
``target_size`` (500) names that E7.8/E7.9 pull in order and the harness (E7.10)
reads to know which names were eligible when.

Rules (all knobs in ``config/ranking.yaml`` → ``backtest.universe``):

1. **Candidates**: ThetaData ``/v3/option/list/symbols`` when the Terminal is up, else
   the cached symbol master's optionable rows (Alpaca optionable ∪ SEC). With
   ``add_inactive_listed`` the builder also adds Alpaca's *inactive* listed-exchange
   assets (delisted names a current list can't contain; a partial survivorship fix,
   counted in the manifest). Index roots (SPX, NDX, VIX, …) are dropped unless
   ``include_index_roots``. ``always`` and the ever-proposed/held names are added.
2. **Stage 1** (free Alpaca SIP daily bars, raw): for each ``Q``, the median dollar
   volume (close × volume) over the last ``dv_window`` sessions **strictly before Q**;
   a name needs ``min_sessions`` bars in that window, a bar within the last
   ``max_stale_sessions`` sessions (alive at Q), and a last close ≥ ``min_price``.
   The top ``stage1_keep`` by dollar volume survive. Bars on or after ``Q`` are never
   read, so a later delisting can't remove a name from the quarters it qualified in.
3. **Stage 2** (options liquidity, ThetaData): per survivor and quarter, near-ATM OI on
   the last session before ``Q`` (Value+ tiers) + the option volume over the
   ``volume_days`` sessions ending there. Quarters with scores rank by the score
   (score ≤ 0 = no listed options then = dropped); quarters without scores (stage-1
   only, or older than the tier serves) rank by stage-1 dollar volume. Stocks and
   ETFs share one list, no quotas.
4. **Always-include**: ``always`` (SPY/QQQ/IWM + the D9 seed) and every ticker in
   ``proposals`` / ``open_structures`` (the audit DB, opened read-only) bypass the
   screens.
5. **Pull list**: buckets 1 always, 2 ever proposed/held, 3 in the top ``target_size``
   in ≥ ``persistent_frac`` of quarters, 4 every other ranked name; inside a bucket
   by median rank. Truncated at ``target_size``; position → checkpoint label
   (``c125`` = 1–125, ``c250`` = 126–250, ``c500`` = 251–500). Buckets 1–2 must fit
   in the first checkpoint or the build fails.
6. **Membership**: ``(quarter, symbol, rank, stage1_dv, stage2_score, …)``; a name is
   eligible in a quarter when ``in_top`` (or ``bypass``) **and** ``in_pull_list``.

Output: ``<data-dir>/backtest_universe/<version>.parquet`` (the pull list, priority
order, a ``symbol`` column so ``arc history download --tickers-file`` reads it),
``<version>.membership.parquet`` and ``<version>.json`` (the manifest).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arc.utils.calendar import ET, sessions_between

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from arc.backtest.underlying import UnderlyingStore
    from arc.universe.master import SymbolMaster

log = structlog.get_logger()

__all__ = [
    "AssetType",
    "Bucket",
    "CheckpointSplit",
    "MembershipRow",
    "QuarterStats",
    "Stage2Config",
    "UniverseBuild",
    "UniverseConfig",
    "UniverseError",
    "UniverseManifest",
    "UniverseRow",
    "build_universe",
    "checkpoint_symbols",
    "classify_asset",
    "ever_traded",
    "format_build",
    "load_bars",
    "load_universe",
    "open_ro",
    "quarter_starts",
    "run_stage2",
    "stage1_quarter",
    "write_build",
]

DIR_NAME = "backtest_universe"
#: Bars fetched before the first quarter so its window is full (60 sessions ≈ 90 days).
WARMUP_CALENDAR_DAYS = 120

D9_SEED: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "AAPL", "MSFT", "NVDA",
    "AMZN", "GOOGL", "META", "TSLA", "AMD", "JPM", "BAC", "XOM", "UNH", "HD",
)  # fmt: skip

#: Option roots that need a separate index subscription (cash-settled index options).
INDEX_ROOTS: tuple[str, ...] = (
    "SPX", "SPXW", "SPXPM", "XSP", "NDX", "NDXP", "XND", "RUT", "RUTW", "MRUT",
    "VIX", "VIXW", "DJX", "OEX", "XEO", "XAU", "SOX", "OSX", "BKX", "HGX",
)  # fmt: skip

DEFAULT_ETF_PATTERN = (
    r"\bETF\b|\bETN\b|\bFUND\b|ISHARES|SPDR|PROSHARES|DIREXION|POWERSHARES|"
    r"SELECT SECTOR|\bQQQ\b|INDEX TRUST|VANECK|GRANITESHARES"
)

AssetType = Literal["etf", "stock", "unknown"]


class UniverseError(RuntimeError):
    """The build can't meet a hard rule (e.g. buckets 1–2 overflow the first checkpoint)."""


class Bucket:
    ALWAYS = 1
    EVER_TRADED = 2
    PERSISTENT = 3
    REST = 4
    NAMES: dict[int, str] = {
        1: "always",
        2: "ever_traded",
        3: "persistent",
        4: "rest",
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_FORBID = ConfigDict(extra="forbid", frozen=True)


class Stage2Config(BaseModel):
    """Options-liquidity screen (ThetaData), per stage-1 survivor and quarter."""

    model_config = _FORBID

    enabled: bool = Field(True, description="Run stage 2 when a Theta Terminal is reachable")
    max_dte: int = Field(60, ge=1)
    strike_range: int = Field(5, ge=1, description="Strikes each side of spot (+ ATM)")
    volume_days: int = Field(20, ge=1, description="Sessions of option volume ending at Q-1")
    oi_weight: float = Field(1.0, ge=0.0)
    volume_weight: float = Field(1.0, ge=0.0)


class UniverseConfig(BaseModel):
    """``backtest.universe`` in ``config/ranking.yaml``."""

    model_config = _FORBID

    target_size: int = Field(500, ge=1, description="Hard cap on the pull list (D84: 500)")
    checkpoints: list[int] = Field(default_factory=lambda: [125, 250, 500])
    start: dt.date = Field(dt.date(2020, 1, 1), description="First quarter start")
    dv_window: int = Field(60, ge=1, description="Sessions in the dollar-volume median")
    min_sessions: int = Field(40, ge=1, description="Bars needed inside the window")
    max_stale_sessions: int = Field(
        5, ge=1, description="The last bar must be within this many sessions of Q"
    )
    min_price: float = Field(10.0, ge=0.0, description="Last close before Q (D9: $10)")
    stage1_keep: int = Field(1500, ge=1)
    persistent_frac: float = Field(0.75, gt=0.0, le=1.0)
    stage2: Stage2Config = Field(default_factory=lambda: Stage2Config())
    always: list[str] = Field(default_factory=lambda: list(D9_SEED))
    include_ever_traded: bool = True
    include_index_roots: bool = False
    index_roots: list[str] = Field(default_factory=lambda: list(INDEX_ROOTS))
    add_inactive_listed: bool = Field(
        True, description="Add Alpaca inactive listed-exchange assets (delisted names)"
    )
    etf_name_pattern: str = DEFAULT_ETF_PATTERN
    fetch_batch: int = Field(100, ge=1, le=1000, description="Symbols per Alpaca bars request")

    @field_validator("always", "index_roots")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(s.strip().upper() for s in v if s.strip()))

    @model_validator(mode="after")
    def _checks(self) -> UniverseConfig:
        cps = self.checkpoints
        if not cps or any(b <= a for a, b in zip(cps, cps[1:], strict=False)) or cps[0] < 1:
            msg = f"checkpoints must be strictly increasing positive sizes, got {cps}"
            raise ValueError(msg)
        if cps[-1] != self.target_size:
            msg = f"the last checkpoint ({cps[-1]}) must equal target_size ({self.target_size})"
            raise ValueError(msg)
        if self.min_sessions > self.dv_window:
            msg = "min_sessions cannot exceed dv_window"
            raise ValueError(msg)
        if self.max_stale_sessions > self.dv_window:
            msg = "max_stale_sessions cannot exceed dv_window"
            raise ValueError(msg)
        re.compile(self.etf_name_pattern)
        return self

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()

    def checkpoint_label(self, position: int) -> str:
        """``c<size>`` of the smallest checkpoint holding 1-based *position*."""
        for cp in self.checkpoints:
            if position <= cp:
                return f"c{cp}"
        msg = f"position {position} is past target_size {self.target_size}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


class UniverseRow(BaseModel):
    """One pull-list name (file order = priority order)."""

    model_config = _FORBID

    position: int = Field(..., ge=1)
    symbol: str
    bucket: int = Field(..., ge=1, le=4)
    bucket_name: str
    asset_type: AssetType
    first_q: dt.date | None = None
    last_q: dt.date | None = None
    quarters_in: int = Field(0, ge=0, description="Quarters in the top target_size")
    quarters_ranked: int = Field(0, ge=0, description="Quarters with any rank")
    median_rank: float | None = None
    checkpoint: str


class MembershipRow(BaseModel):
    """One (quarter, symbol): the point-in-time screen's view at quarter start."""

    model_config = _FORBID

    quarter: dt.date
    symbol: str
    rank: int | None = None
    stage1_dv: float | None = None
    stage2_score: float | None = None
    in_top: bool = False
    bypass: bool = False
    in_pull_list: bool = False


class QuarterStats(BaseModel):
    model_config = _FORBID

    quarter: dt.date
    with_bars: int = Field(0, description="Candidates alive with enough bars in the window")
    priced: int = Field(0, description="... and last close >= min_price")
    stage1_kept: int = 0
    stage2_scored: int = 0
    ranked: int = 0
    top: int = 0
    top_outside_list: int = Field(0, description="Top-N names not in the final pull list")
    rank_basis: Literal["stage1_dv", "stage2_score"] = "stage1_dv"


class CheckpointSplit(BaseModel):
    model_config = _FORBID

    checkpoint: str
    names: int
    etf: int
    stock: int
    unknown: int
    by_bucket: dict[str, int]


class UniverseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    built_at: dt.datetime
    git_sha: str | None = None
    config_hash: str
    config: dict[str, object]
    as_of: dt.date
    stage1_only: bool
    candidate_source: str
    candidate_count: int
    candidates_by_source: dict[str, int] = Field(default_factory=dict)
    excluded_index_roots: list[str] = Field(default_factory=list)
    always: list[str] = Field(default_factory=list)
    ever_traded: list[str] = Field(default_factory=list)
    quarters: list[QuarterStats] = Field(default_factory=list)
    union_size: int = Field(0, description="Distinct names ever in a quarter's top N + bypass")
    ranked_size: int = Field(0, description="Distinct names ever ranked (stage-1 kept)")
    pull_list_size: int = 0
    missing_underlying: list[str] = Field(
        default_factory=list, description="Candidates with no Alpaca bars (survivorship gap)"
    )
    checkpoints: list[CheckpointSplit] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class UniverseBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[UniverseRow]
    membership: list[MembershipRow]
    manifest: UniverseManifest


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def classify_asset(name: str | None, pattern: str = DEFAULT_ETF_PATTERN) -> AssetType:
    """``etf`` / ``stock`` from the security name; ``unknown`` without a name."""
    if not name:
        return "unknown"
    return "etf" if re.search(pattern, name.upper()) else "stock"


def master_candidates(master: SymbolMaster) -> dict[str, str]:
    """Optionable symbol-master rows → ``{symbol: name}``."""
    return {s: info.name for s, info in master.symbols.items() if info.options}


def open_ro(db_path: Path) -> sqlite3.Connection:
    """Open the audit store read-only (``mode=ro``); never creates or writes it."""
    return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)


def ever_traded(db_path: Path) -> list[str]:
    """Distinct tickers of ``proposals`` ∪ ``open_structures`` (read-only open)."""
    conn = open_ro(db_path)
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        wanted = [t for t in ("proposals", "open_structures") if t in tables]
        if not wanted:
            return []
        sql = " union ".join(f"select distinct ticker from {t}" for t in wanted)  # noqa: S608
        return sorted({str(r[0]).strip().upper() for r in conn.execute(sql) if r[0]})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bars (raw daily close + volume)
# ---------------------------------------------------------------------------


class DailyBarsBatchSource(Protocol):
    def daily_bars(
        self, symbols: Sequence[str], start: dt.date, end: dt.date
    ) -> dict[str, pd.DataFrame]:
        """``{symbol: DataFrame[close, volume]}`` indexed by session date; absent = no bars."""
        ...


class AlpacaDailyBarsBatch:  # pragma: no cover - network
    """Alpaca SIP daily bars (raw, unadjusted), many symbols per request.

    The default ``asof`` maps a renamed symbol's history onto today's symbol (FB → META),
    so a current name has one continuous series. A batch the API rejects is bisected
    until the offending symbol is isolated (that symbol counts as missing).
    """

    def __init__(self) -> None:
        from alpaca.data.historical import StockHistoricalDataClient

        from arc.data.alpaca import _get_keys

        key, secret = _get_keys()
        self._client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    def daily_bars(
        self, symbols: Sequence[str], start: dt.date, end: dt.date
    ) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if not symbols:
            return {}
        req = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            start=dt.datetime.combine(start, dt.time.min, tzinfo=ET),
            end=dt.datetime.combine(end, dt.time.max, tzinfo=ET),
            timeframe=TimeFrame.Day,
            feed=DataFeed.SIP,
            adjustment=Adjustment.RAW,
        )
        try:
            data = getattr(self._client.get_stock_bars(req), "data", {})
        except Exception as exc:
            if len(symbols) == 1:
                log.warning("universe.bars_failed", symbol=symbols[0], error=str(exc)[:200])
                return {}
            mid = len(symbols) // 2
            return {
                **self.daily_bars(symbols[:mid], start, end),
                **self.daily_bars(symbols[mid:], start, end),
            }
        out: dict[str, pd.DataFrame] = {}
        for sym, bars in data.items():
            if not bars:
                continue
            idx = [b.timestamp.astimezone(ET).date() for b in bars]
            out[sym] = pd.DataFrame(
                {"close": [b.close for b in bars], "volume": [b.volume for b in bars]},
                index=idx,
                dtype=float,
            )
        return out


def _covers(bars: pd.DataFrame, start: dt.date, end: dt.date) -> bool:
    if bars.empty or bars["volume"].isna().all():
        return False
    idx = pd.Index(bars.index)
    return bool(min(idx) <= start and max(idx) >= end)


def _read_fetched(path: Path | None) -> dict[str, tuple[dt.date, dt.date]]:
    if path is None or not path.is_file():
        return {}
    raw = json.loads(path.read_text())
    return {s: (dt.date.fromisoformat(v[0]), dt.date.fromisoformat(v[1])) for s, v in raw.items()}


def _write_fetched(path: Path | None, fetched: Mapping[str, tuple[dt.date, dt.date]]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {s: [a.isoformat(), b.isoformat()] for s, (a, b) in sorted(fetched.items())}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(blob, indent=0) + "\n")
    tmp.replace(path)


def load_bars(
    store: UnderlyingStore,
    symbols: Sequence[str],
    start: dt.date,
    end: dt.date,
    source: DailyBarsBatchSource | None,
    *,
    batch: int = 100,
    fetched_path: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Close + volume for *symbols* over [start, end]: cache first, then *source* in batches.

    A symbol is not re-fetched when its cached bars (with volume) span the range, or
    when *fetched_path* (a JSON ledger of the ranges already asked for) says this range
    was already requested: a delisted name's bars end early and a name Alpaca never
    had returns nothing, and asking again can't change either. Fetched bars merge into
    ``<data-dir>/underlying_daily/`` (fetched rows win). Returns the bars and the
    symbols with no bars at all (sorted).
    """
    syms = list(dict.fromkeys(x.upper() for x in symbols))
    fetched = _read_fetched(fetched_path)
    out: dict[str, pd.DataFrame] = {}
    need: list[str] = []
    for s in syms:
        cached = store.read_bars(s)
        rng = fetched.get(s)
        asked = rng is not None and rng[0] <= start and rng[1] >= end
        if _covers(cached, start, end) or asked:
            if not cached.empty and not cached["volume"].isna().all():
                out[s] = cached
        else:
            need.append(s)
    if source is None:
        for s in need:
            cached = store.read_bars(s)
            if not cached.empty and not cached["volume"].isna().all():
                out[s] = cached
    else:
        for i in range(0, len(need), batch):
            chunk = need[i : i + batch]
            got = source.daily_bars(chunk, start, end)
            for s in chunk:
                fetched[s] = (start, end)
                fresh = got.get(s)
                if fresh is None or fresh.empty:
                    continue
                old = store.read_bars(s)
                merged = pd.concat([old, fresh]) if not old.empty else fresh
                merged = merged[~pd.Index(merged.index).duplicated(keep="last")].sort_index()
                store.write_bars(s, merged)
                out[s] = merged
            _write_fetched(fetched_path, fetched)
            if progress is not None:
                progress(min(i + batch, len(need)), len(need))
    missing = sorted(s for s in syms if s not in out)
    return out, missing


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------


def quarter_starts(start: dt.date, as_of: dt.date) -> list[dt.date]:
    """Calendar quarter starts from *start*'s quarter through the last one ≤ *as_of*."""
    q = dt.date(start.year, 3 * ((start.month - 1) // 3) + 1, 1)
    if q < start:  # start inside a quarter: the first full quarter after it
        q = _next_quarter(q)
    out: list[dt.date] = []
    while q <= as_of:
        out.append(q)
        q = _next_quarter(q)
    return out


def _next_quarter(q: dt.date) -> dt.date:
    return dt.date(q.year + 1, 1, 1) if q.month == 10 else dt.date(q.year, q.month + 3, 1)


class Stage1Rec(BaseModel):
    model_config = _FORBID

    symbol: str
    dv: float
    close: float
    priced: bool


def stage1_quarter(
    bars: Mapping[str, pd.DataFrame],
    window: Sequence[dt.date],
    cfg: UniverseConfig,
) -> list[Stage1Rec]:
    """Every candidate alive in *window* (the sessions before Q) with its median $-volume.

    Reads bars on the *window* dates only, so nothing on/after Q can change the result.
    """
    if not window:
        return []
    win = pd.Index(window)
    recent = set(window[-cfg.max_stale_sessions :])
    out: list[Stage1Rec] = []
    for sym, df in bars.items():
        if df.empty:
            continue
        sub = df.loc[pd.Index(df.index).isin(win)].dropna(subset=["close", "volume"])
        if len(sub) < cfg.min_sessions:
            continue
        last = max(sub.index)
        if last not in recent:
            continue
        dv = float(np.median(sub["close"].to_numpy() * sub["volume"].to_numpy()))
        close = float(sub.loc[last, "close"])
        out.append(Stage1Rec(symbol=sym, dv=dv, close=close, priced=close >= cfg.min_price))
    return out


def _window(sessions: Sequence[dt.date], q: dt.date, n: int) -> list[dt.date]:
    before = [d for d in sessions if d < q]
    return before[-n:]


def stage1_survivors(
    candidates: Iterable[str],
    bars: Mapping[str, pd.DataFrame],
    cfg: UniverseConfig,
    as_of: dt.date,
    sessions: Sequence[dt.date],
) -> dict[dt.date, list[str]]:
    """Per quarter: the stage-1 kept names (priced, top ``stage1_keep`` by $-volume)."""
    cands = [s for s in candidates if s in bars]
    out: dict[dt.date, list[str]] = {}
    for q in quarter_starts(cfg.start, as_of):
        recs = stage1_quarter({s: bars[s] for s in cands}, _window(sessions, q, cfg.dv_window), cfg)
        priced = sorted((r for r in recs if r.priced), key=lambda r: (-r.dv, r.symbol))
        out[q] = [r.symbol for r in priced[: cfg.stage1_keep]]
    return out


def build_universe(
    *,
    candidates: Mapping[str, str | None],
    bars: Mapping[str, pd.DataFrame],
    cfg: UniverseConfig,
    as_of: dt.date,
    built_at: dt.datetime,
    sessions: Sequence[dt.date] | None = None,
    ever: Sequence[str] = (),
    stage2_scores: Mapping[tuple[dt.date, str], float] | None = None,
    stage1_only: bool = False,
    missing_underlying: Sequence[str] = (),
    candidate_source: str = "symbol_master",
    candidates_by_source: Mapping[str, int] | None = None,
    excluded_index_roots: Sequence[str] = (),
    git_sha: str | None = None,
) -> UniverseBuild:
    """The deterministic universe build from already-loaded inputs (no I/O).

    *candidates* maps symbol → security name (``None`` = unknown). *bars* holds raw
    close + volume per symbol. *stage2_scores* maps ``(quarter, symbol)`` → score;
    a quarter with any score ranks by score, the rest by stage-1 dollar volume.
    """
    quarters = quarter_starts(cfg.start, as_of)
    if sessions is None:
        lo = cfg.start - dt.timedelta(days=WARMUP_CALENDAR_DAYS)
        sessions = sessions_between(lo, as_of)
    scores = {} if stage1_only or stage2_scores is None else dict(stage2_scores)
    always = list(cfg.always)
    ever_list = [s for s in dict.fromkeys(x.upper() for x in ever) if s not in set(always)]
    if not cfg.include_ever_traded:
        ever_list = []
    bypass = set(always) | set(ever_list)

    membership: list[MembershipRow] = []
    stats: list[QuarterStats] = []
    ranks: dict[str, list[int]] = defaultdict(list)
    tops: dict[str, list[dt.date]] = defaultdict(list)
    for q in quarters:
        window = _window(sessions, q, cfg.dv_window)
        recs = stage1_quarter({s: bars[s] for s in candidates if s in bars}, window, cfg)
        priced = sorted((r for r in recs if r.priced), key=lambda r: (-r.dv, r.symbol))
        kept = priced[: cfg.stage1_keep]
        q_scores = {s: v for (qq, s), v in scores.items() if qq == q}
        basis: Literal["stage1_dv", "stage2_score"] = "stage2_score" if q_scores else "stage1_dv"
        if q_scores:
            scored = [r for r in kept if q_scores.get(r.symbol, 0.0) > 0.0]
            ordered = sorted(scored, key=lambda r: (-q_scores[r.symbol], -r.dv, r.symbol))
        else:
            ordered = kept
        rank_of = {r.symbol: i + 1 for i, r in enumerate(ordered)}
        dv_of = {r.symbol: r.dv for r in recs}
        top = {s for s, rk in rank_of.items() if rk <= cfg.target_size}
        for s, rk in rank_of.items():
            ranks[s].append(rk)
            if s in top:
                tops[s].append(q)
        for s in sorted(set(rank_of) | bypass):
            membership.append(
                MembershipRow(
                    quarter=q,
                    symbol=s,
                    rank=rank_of.get(s),
                    stage1_dv=dv_of.get(s),
                    stage2_score=q_scores.get(s),
                    in_top=s in top,
                    bypass=s in bypass,
                )
            )
        stats.append(
            QuarterStats(
                quarter=q,
                with_bars=len(recs),
                priced=len(priced),
                stage1_kept=len(kept),
                stage2_scored=sum(1 for r in kept if r.symbol in q_scores),
                ranked=len(ordered),
                top=len(top),
                rank_basis=basis,
            )
        )

    n_q = len(quarters)

    def med(s: str) -> float | None:
        return float(statistics.median(ranks[s])) if ranks.get(s) else None

    def key(s: str) -> tuple[bool, float, str]:
        m = med(s)
        return (m is None, m if m is not None else math.inf, s)

    b1 = sorted(always, key=key)
    b2 = sorted(ever_list, key=key)
    if len(b1) + len(b2) > cfg.checkpoints[0]:
        msg = (
            f"buckets 1-2 (always {len(b1)} + ever proposed/held {len(b2)} = "
            f"{len(b1) + len(b2)}) exceed the first checkpoint c{cfg.checkpoints[0]}"
        )
        raise UniverseError(msg)
    rest = [s for s in ranks if s not in bypass]
    b3 = sorted(
        (s for s in rest if n_q and len(tops.get(s, [])) / n_q >= cfg.persistent_frac), key=key
    )
    b3_set = set(b3)
    b4 = sorted((s for s in rest if s not in b3_set), key=key)
    ordered_all = (
        [(s, Bucket.ALWAYS) for s in b1]
        + [(s, Bucket.EVER_TRADED) for s in b2]
        + [(s, Bucket.PERSISTENT) for s in b3]
        + [(s, Bucket.REST) for s in b4]
    )[: cfg.target_size]

    rows: list[UniverseRow] = []
    for i, (s, b) in enumerate(ordered_all, start=1):
        qs = tops.get(s, [])
        rows.append(
            UniverseRow(
                position=i,
                symbol=s,
                bucket=b,
                bucket_name=Bucket.NAMES[b],
                asset_type=classify_asset(candidates.get(s), cfg.etf_name_pattern),
                first_q=min(qs) if qs else None,
                last_q=max(qs) if qs else None,
                quarters_in=len(qs),
                quarters_ranked=len(ranks.get(s, [])),
                median_rank=med(s),
                checkpoint=cfg.checkpoint_label(i),
            )
        )
    in_list = {r.symbol for r in rows}
    membership = [m.model_copy(update={"in_pull_list": m.symbol in in_list}) for m in membership]
    gap: dict[dt.date, int] = defaultdict(int)
    for m in membership:
        if m.in_top and not m.in_pull_list:
            gap[m.quarter] += 1
    stats = [s.model_copy(update={"top_outside_list": gap.get(s.quarter, 0)}) for s in stats]

    splits: list[CheckpointSplit] = []
    for cp in cfg.checkpoints:
        sub = [r for r in rows if r.position <= cp]
        by_bucket: dict[str, int] = defaultdict(int)
        for r in sub:
            by_bucket[r.bucket_name] += 1
        splits.append(
            CheckpointSplit(
                checkpoint=f"c{cp}",
                names=len(sub),
                etf=sum(1 for r in sub if r.asset_type == "etf"),
                stock=sum(1 for r in sub if r.asset_type == "stock"),
                unknown=sum(1 for r in sub if r.asset_type == "unknown"),
                by_bucket=dict(by_bucket),
            )
        )
    notes: list[str] = []
    if len(rows) < cfg.target_size:
        notes.append(f"pull list has {len(rows)} names, short of target_size {cfg.target_size}")
    if stage1_only:
        notes.append("stage 1 only: every quarter ranks by underlying dollar volume")
    chash = cfg.config_hash()
    version = f"u{cfg.target_size}-{as_of:%Y%m%d}-{'s1' if stage1_only else 's2'}-{chash[:8]}"
    manifest = UniverseManifest(
        version=version,
        built_at=built_at,
        git_sha=git_sha,
        config_hash=chash,
        config=cfg.model_dump(mode="json"),
        as_of=as_of,
        stage1_only=stage1_only,
        candidate_source=candidate_source,
        candidate_count=len(candidates),
        candidates_by_source=dict(candidates_by_source or {}),
        excluded_index_roots=sorted(excluded_index_roots),
        always=list(always),
        ever_traded=list(ever_list),
        quarters=stats,
        union_size=len(set(tops) | bypass),
        ranked_size=len(ranks),
        pull_list_size=len(rows),
        missing_underlying=sorted(missing_underlying),
        checkpoints=splits,
        notes=notes,
    )
    return UniverseBuild(rows=rows, membership=membership, manifest=manifest)


# ---------------------------------------------------------------------------
# Stage 2 (ThetaData options liquidity)
# ---------------------------------------------------------------------------


class OptionsProbe(Protocol):
    """The two ThetaData calls stage 2 needs (``ThetaDataEodProvider`` satisfies it)."""

    def earliest_date(self) -> dt.date: ...

    def fetch_option_eod(
        self,
        underlying: str,
        start: dt.date,
        end: dt.date,
        *,
        max_dte: int,
        strike_range: int | None = None,
    ) -> Sequence[object]: ...

    def fetch_open_interest(
        self,
        underlying: str,
        start: dt.date,
        end: dt.date,
        *,
        max_dte: int,
        strike_range: int | None = None,
    ) -> Mapping[object, float]: ...


def stage2_score(
    probe: OptionsProbe,
    symbol: str,
    window: Sequence[dt.date],
    cfg: Stage2Config,
    *,
    with_oi: bool,
) -> float:
    """Near-ATM OI on the last session before Q + option volume over ``volume_days``."""
    last = window[-1]
    first = window[-cfg.volume_days] if len(window) >= cfg.volume_days else window[0]
    rows = probe.fetch_option_eod(
        symbol, first, last, max_dte=cfg.max_dte, strike_range=cfg.strike_range
    )
    volume = sum(float(getattr(r, "volume", 0.0) or 0.0) for r in rows)
    oi = 0.0
    if with_oi:
        got = probe.fetch_open_interest(
            symbol, last, last, max_dte=cfg.max_dte, strike_range=cfg.strike_range
        )
        oi = float(sum(got.values()))
    return cfg.oi_weight * oi + cfg.volume_weight * volume


def run_stage2(
    probe: OptionsProbe,
    survivors: Mapping[dt.date, Sequence[str]],
    sessions: Sequence[dt.date],
    cfg: UniverseConfig,
    *,
    with_oi: bool,
    cache_path: Path,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[tuple[dt.date, str], float], list[dt.date]]:
    """Score every (quarter, survivor); resumable via a parquet cache at *cache_path*.

    Quarters whose last pre-Q session is older than ``probe.earliest_date()`` are
    skipped (they need the paid month, E7.8) and returned as the second value.
    """
    cache = _read_scores(cache_path)
    earliest = probe.earliest_date()
    skipped: list[dt.date] = []
    done = 0
    for q, syms in sorted(survivors.items()):
        window = _window(sessions, q, max(cfg.stage2.volume_days, 1))
        if not window or window[-1] < earliest:
            skipped.append(q)
            continue
        for s in syms:
            if (q, s) in cache:
                continue
            try:
                cache[(q, s)] = stage2_score(probe, s, window, cfg.stage2, with_oi=with_oi)
            except Exception as exc:  # noqa: BLE001 - one bad root must not stop the build
                log.warning(
                    "universe.stage2_failed", symbol=s, quarter=q.isoformat(), error=str(exc)[:200]
                )
                cache[(q, s)] = 0.0
            done += 1
            if done % 50 == 0:
                _write_scores(cache_path, cache)
                if progress is not None:
                    progress(f"stage2: {done} probes (quarter {q})")
    _write_scores(cache_path, cache)
    wanted = {(q, s) for q, syms in survivors.items() if q not in skipped for s in syms}
    return {k: v for k, v in cache.items() if k in wanted}, skipped


def _read_scores(path: Path) -> dict[tuple[dt.date, str], float]:
    if not path.is_file():
        return {}
    df = pd.read_parquet(path)
    return {
        (q, str(s)): float(v)
        for q, s, v in zip(df["quarter"], df["symbol"], df["score"], strict=True)
    }


def _write_scores(path: Path, scores: Mapping[tuple[dt.date, str], float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = sorted(scores.items())
    df = pd.DataFrame(
        {
            "quarter": [k[0] for k, _ in items],
            "symbol": [k[1] for k, _ in items],
            "score": [v for _, v in items],
        }
    )
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def universe_dir(data_dir: Path) -> Path:
    return Path(data_dir) / DIR_NAME


def write_build(build: UniverseBuild, data_dir: Path) -> dict[str, Path]:
    """Write the pull list, the membership table and the manifest; returns their paths."""
    out = universe_dir(data_dir)
    out.mkdir(parents=True, exist_ok=True)
    v = build.manifest.version
    paths = {
        "pull_list": out / f"{v}.parquet",
        "membership": out / f"{v}.membership.parquet",
        "manifest": out / f"{v}.json",
    }
    pd.DataFrame([r.model_dump() for r in build.rows]).to_parquet(paths["pull_list"], index=False)
    pd.DataFrame([m.model_dump() for m in build.membership]).to_parquet(
        paths["membership"], index=False
    )
    paths["manifest"].write_text(build.manifest.model_dump_json(indent=2) + "\n")
    return paths


def _latest_version(data_dir: Path) -> str:
    out = universe_dir(data_dir)
    manifests = sorted(out.glob("*.json")) if out.is_dir() else []
    if not manifests:
        msg = f"no universe built under {out} (run `arc history universe build`)"
        raise FileNotFoundError(msg)
    best = max(
        manifests,
        key=lambda p: UniverseManifest.model_validate_json(p.read_text()).built_at,
    )
    return best.stem


def load_universe(
    data_dir: Path, version: str | None = None
) -> tuple[UniverseManifest, list[UniverseRow], pd.DataFrame]:
    """(manifest, pull-list rows in priority order, membership frame) of *version* (latest)."""
    v = version or _latest_version(data_dir)
    out = universe_dir(data_dir)
    manifest = UniverseManifest.model_validate_json((out / f"{v}.json").read_text())
    df = pd.read_parquet(out / f"{v}.parquet")
    rows = [
        UniverseRow.model_validate({k: _none(val) for k, val in rec.items()})
        for rec in df.to_dict(orient="records")
    ]
    rows.sort(key=lambda r: r.position)
    membership = pd.read_parquet(out / f"{v}.membership.parquet")
    return manifest, rows, membership


def _none(v: object) -> object:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    return v


def checkpoint_symbols(rows: Iterable[UniverseRow], checkpoint: str) -> list[str]:
    """Symbols of ``c125`` / ``c250`` / ``c500`` (nested: c250 includes c125)."""
    m = re.fullmatch(r"c(\d+)", checkpoint.lower())
    if not m:
        msg = f"checkpoint must look like c125, got {checkpoint!r}"
        raise ValueError(msg)
    size = int(m.group(1))
    return [r.symbol for r in sorted(rows, key=lambda r: r.position) if r.position <= size]


def format_build(build: UniverseBuild, *, top: int = 40) -> str:
    """Plain-text report: per-quarter counts, union size, checkpoint split, first rows."""
    m = build.manifest
    lines = [
        f"universe {m.version}  as_of {m.as_of}  built {m.built_at:%Y-%m-%d %H:%M %Z}",
        f"candidates {m.candidate_count} ({m.candidate_source}; "
        + ", ".join(f"{k} {v}" for k, v in sorted(m.candidates_by_source.items()))
        + f")  missing_underlying {len(m.missing_underlying)}"
        + f"  index roots excluded {len(m.excluded_index_roots)}",
        f"always {len(m.always)}  ever proposed/held {len(m.ever_traded)}",
        "",
        "quarter     with_bars  priced  kept  scored  ranked  top  top_outside_list  basis",
    ]
    for q in m.quarters:
        lines.append(
            f"{q.quarter}  {q.with_bars:9d}  {q.priced:6d}  {q.stage1_kept:4d}  "
            f"{q.stage2_scored:6d}  {q.ranked:6d}  {q.top:3d}  {q.top_outside_list:16d}  "
            f"{q.rank_basis}"
        )
    lines += [
        "",
        f"union (ever in a quarter's top {m.config['target_size']} + bypass): {m.union_size}"
        f"   ever ranked: {m.ranked_size}   pull list: {m.pull_list_size}",
        "",
        "checkpoint  names  etf  stock  unknown  by bucket",
    ]
    for c in m.checkpoints:
        bb = ", ".join(f"{k} {v}" for k, v in c.by_bucket.items())
        lines.append(
            f"{c.checkpoint:10s}  {c.names:5d}  {c.etf:3d}  {c.stock:5d}  {c.unknown:7d}  {bb}"
        )
    lines += ["", f"first {min(top, len(build.rows))} rows:", _rows_table(build.rows[:top])]
    lines += [f"note: {n}" for n in m.notes]
    return "\n".join(lines)


def _rows_table(rows: Sequence[UniverseRow]) -> str:
    out = ["pos  symbol  bucket       type     first_q     last_q      q_in  median_rank  cp"]
    for r in rows:
        mr = f"{r.median_rank:.1f}" if r.median_rank is not None else "-"
        out.append(
            f"{r.position:3d}  {r.symbol:6s}  {r.bucket}:{r.bucket_name:11s} {r.asset_type:7s}  "
            f"{r.first_q or '-'!s:10s}  {r.last_q or '-'!s:10s}  {r.quarters_in:4d}  "
            f"{mr:>11s}  {r.checkpoint}"
        )
    return "\n".join(out)
