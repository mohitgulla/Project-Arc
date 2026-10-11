"""Trending tier v2 (D58, card E13.19): rank the daily ``retail_buzz`` pull into a tier.

Deterministic code, no LLM, no network: the ``retail_buzz`` source (Reddit via
ApeWisdom + Stocktwits, :mod:`arc.ingest.retail_buzz`) stores raw rows once a day;
this module scores them and builds the ``universe_tier`` entry (subject ``trending``).

Per input (restored from E12.3, ``f0101ea:arc/universe/trending.py``):

* ``apewisdom`` (Reddit): the mean of the rank-normalised mentions and 24 h rank gain
  (``scoring: rank_gain``, the default). E14.5 (D60): ``scoring: velocity`` (flag
  ``universe.trending.scoring``, a runtime-tunable registry key) swaps the rank gain for the
  rank-normalised mention velocity (:func:`mention_velocity`); a row without one scores
  0 on that half (never infinite growth).
* ``stocktwits``: ``trending_score`` (list position when absent); crypto and non-US
  symbols dropped.

Each input's raw values are rank-normalised (:func:`rank_normalise`) to ``(0, 1]``.

Rules (:func:`rank_trending`, pure):

1. Eligible = in the symbol master, optionable, tradable, listed. Excluded before the
   cut: names already in core / momentum / discovery, the market reference
   (SPY/QQQ/IWM), share-class aliases of excluded names and leveraged / inverse ETFs
   (:func:`leveraged`; journaled ``universe:trending_leveraged``).
2. ``trend_score`` = sum of input scores / number of **enabled** inputs. A missing or
   stale input (the ``retail_buzz`` entry older than its category's ``max_age``)
   contributes 0; the others are never renormalised.
3. Order: names in at least ``min_inputs_first`` (2) inputs first, by ``trend_score``
   desc then ticker; then the single-input names, same order. The first ``pool`` (40)
   go to the tier's screen; the first ``universe_trending_size`` (25) passes are the
   tier, each with ``inputs`` (2|1) and a reason ``reddit #r · stocktwits #s · score x``.
4. Zero live inputs = :class:`TrendingError` (nothing written; the tier is empty that
   day). One live input still builds (single-input names only); the notice names the
   missing input.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.universe.master import normalize_symbol
from arc.universe.velocity import (
    Velocity,
    VelocityOptions,
    buzz_velocities,
    format_velocity,
    mention_velocity,
    velocity_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from arc.context.kinds import RetailBuzzInput, RetailBuzzPayload
    from arc.universe.master import SymbolMaster
    from arc.universe.screen import ScreenResult
    from arc.universe.tiers import UniverseTierPayload

log = structlog.get_logger(__name__)

__all__ = [
    "InputResult",
    "TrendingError",
    "TrendingOptions",
    "TrendingResult",
    "TrendingRow",
    "VelocityOptions",
    "build_payload",
    "buzz_velocities",
    "fastest_risers",
    "format_velocity",
    "eligible",
    "journal_decisions",
    "latest_buzz",
    "leveraged",
    "mention_velocity",
    "notice_line",
    "previous_members",
    "rank_normalise",
    "rank_trending",
    "run_trending",
    "score_inputs",
    "screen_pool",
    "table",
    "velocity_text",
]

EXCLUDED_LEVERAGED = "leveraged"

#: Input name -> short label used in reasons (``reddit #3``); unknown names print as is.
_SHORT = {"apewisdom": "reddit", "stocktwits": "stocktwits"}


class TrendingError(RuntimeError):
    """The trending tier cannot be built today (nothing is written)."""


TrendingScoring = Literal["rank_gain", "velocity"]
#: E14.5: ``universe.trending.scoring`` choices, the control first.
SCORING_CHOICES: tuple[str, ...] = ("rank_gain", "velocity")


class TrendingOptions(BaseModel):
    """``universe.trending`` job options (``config/routines.yaml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pool: int = Field(40, ge=1, le=200)
    min_inputs_first: int = Field(2, ge=1, le=10)
    exclude_market_reference: bool = True
    # E14.5 (D60): Reddit's second half; rank_gain = the E13.19 ranking (control).
    scoring: TrendingScoring = "rank_gain"
    velocity: VelocityOptions = Field(default_factory=VelocityOptions)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> TrendingOptions:
        keys = set(cls.model_fields)
        return cls.model_validate({k: v for k, v in options.items() if k in keys})


# ---------------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------------


def rank_normalise(raw: Mapping[str, float]) -> dict[str, float]:
    """Scores in ``(0, 1]`` from raw values (higher = better), by rank.

    The best value scores 1, the worst ``1/n``; tied values share the mean of their
    ranks, so they score equally. Deterministic and scale-free.
    """
    n = len(raw)
    if n == 0:
        return {}
    ordered = sorted(raw.items(), key=lambda kv: (-kv[1], kv[0]))
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        mean_rank = (i + j) / 2 + 1  # 1-based
        score = (n - mean_rank + 1) / n
        for k in range(i, j + 1):
            out[ordered[k][0]] = round(score, 6)
        i = j + 1
    return out


#: E12.3: exchanges a trending name may list on (the symbol master's labels).
LISTED_EXCHANGES: frozenset[str] = frozenset({"NASDAQ", "NYSE", "ARCA", "BATS", "CBOE", "AMEX"})

# D58 rule 1: leveraged / inverse ETFs. The symbol master has no leverage flag, so the
# name decides: a leverage word AND a fund word (so "Ultra Clean Holdings" and
# "Build-A-Bear Workshop" stay eligible; "Direxion Daily Semiconductor Bear 3X ETF",
# "ProShares UltraPro QQQ" and "GraniteShares 2x Long NVDA Daily ETF" do not).
_LEVERAGE_WORD = re.compile(
    r"\b(\d(\.\d+)?x|ultra\w*|bear|bull|inverse|short|leveraged)\b", re.IGNORECASE
)
_FUND_WORD = re.compile(
    r"\b(etf|etn|fund|trust|shares|proshares|direxion|graniteshares)\b", re.IGNORECASE
)


def leveraged(name: str) -> bool:
    """``True`` when a symbol-master *name* reads as a leveraged / inverse fund."""
    return bool(_LEVERAGE_WORD.search(name) and _FUND_WORD.search(name))


def eligible(sym: str, master: SymbolMaster) -> str | None:
    """``None`` when *sym* may be a trending name, else why not."""
    info = master.get(sym)
    if info is None:
        return "not in the symbol master"
    if info.tradable is False:
        return "not tradable at the broker"
    if info.options is not True:
        return "no listed options at the broker"
    if (info.exchange or "").upper() not in LISTED_EXCHANGES:
        return f"not on a listed exchange ({info.exchange or 'unknown'})"
    return None


@dataclass
class InputResult:
    """One input's scores (eligible names only) + audit data."""

    name: str
    type: str
    status: Literal["ok", "stale", "failed", "missing", "empty"]
    raw: dict[str, float] = field(default_factory=dict)
    detail: dict[str, str] = field(default_factory=dict)  # ticker -> reason fragment
    error: str | None = None
    urls: list[str] = field(default_factory=list)
    digest: str = ""
    count: int = 0  # rows read before eligibility
    fetched_at: str | None = None
    dropped: dict[str, str] = field(default_factory=dict)  # ticker -> why not eligible

    @property
    def live(self) -> bool:
        return self.status == "ok" and bool(self.raw)

    def scores(self) -> dict[str, float]:
        return rank_normalise(self.raw) if self.live else {}


def _short(name: str, type_: str) -> str:
    return _SHORT.get(type_, name)


def apewisdom_scores(
    name: str,
    inp: RetailBuzzInput,
    *,
    scoring: TrendingScoring = "rank_gain",
    velocity: VelocityOptions | None = None,
) -> InputResult:
    """Reddit (ApeWisdom): mean of rank-normalised mentions and either the 24 h rank
    gain (``rank_gain``) or the mention velocity (``velocity``, E14.5; a row with no
    velocity scores 0 on that half)."""
    res = _base(name, inp)
    rows = inp.rows
    n = len(rows)
    mentions = {r.symbol: float(r.mentions or 0.0) for r in rows}
    if scoring == "velocity":
        vopts = velocity or VelocityOptions()
        vel = {
            r.symbol: v
            for r in rows
            if (v := mention_velocity(r.mentions, r.mentions_24h_ago, vopts)) is not None
        }
        v_norm = rank_normalise(vel)
        second = {r.symbol: v_norm.get(r.symbol, 0.0) for r in rows}
    else:

        def gain(rank: int | None, prev: int | None) -> float:
            return float((prev if prev else n + 1) - (rank or n))

        second = rank_normalise({r.symbol: gain(r.rank, r.rank_24h_ago) for r in rows})
    m_norm = rank_normalise(mentions)
    for r in rows:
        res.raw[r.symbol] = (m_norm[r.symbol] + second[r.symbol]) / 2
        res.detail[r.symbol] = f"reddit #{r.rank or r.position}"
    return res


def fastest_risers(
    buzz: RetailBuzzPayload | None,
    opts: VelocityOptions,
    *,
    top: int = 5,
    stop_words: Sequence[str] = (),
) -> list[tuple[str, float, float, float]]:
    """The *top* Reddit names by mention velocity: ``(symbol, velocity, m, m24)`` (pure).

    Only rows with a velocity above 1 (rising; >= ``min_mentions`` and a 24 h count
    present); leveraged /
    inverse funds (:func:`leveraged` on the input's name) and word tickers (*stop_words*,
    ``universe.extraction.stop_words``) are left out. Ties: more mentions, then ticker.
    """
    stop = {w.upper() for w in stop_words}
    names: dict[str, str] = {}
    if buzz is not None:
        for inp in buzz.inputs.values():
            if inp.type == "apewisdom":
                for r in inp.rows:
                    names.setdefault(r.symbol, r.name)
    rows = [
        (sym, v, float(m or 0.0), float(m24 or 0.0))
        for sym, (v, m, m24) in buzz_velocities(buzz, opts).items()
        if v is not None and v > 1.0 and sym not in stop and not leveraged(names.get(sym, ""))
    ]
    rows.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return rows[: max(0, top)]


def stocktwits_scores(name: str, inp: RetailBuzzInput) -> InputResult:
    """Stocktwits by ``trending_score`` (position when absent); crypto/non-US dropped."""
    res = _base(name, inp)
    n = len(inp.rows)
    for r in inp.rows:
        exch = (r.exchange or "").upper()
        region = (r.region or "US").upper()
        if exch == "CRYPTO" or region != "US":
            res.dropped[r.symbol] = f"{exch or region} symbol"
            continue
        score = r.trending_score
        res.raw[r.symbol] = float(score) if score is not None else float(n - r.position + 1)
        res.detail[r.symbol] = f"stocktwits #{r.rank or r.position}"
    return res


def _base(name: str, inp: RetailBuzzInput) -> InputResult:
    return InputResult(
        name=name,
        type=inp.type,
        status="ok" if inp.status == "ok" else "failed",
        error=inp.error,
        urls=list(inp.urls),
        digest=inp.digest,
        count=len(inp.rows),
        fetched_at=inp.fetched_at,
    )


def score_inputs(
    buzz: RetailBuzzPayload | None,
    enabled: Sequence[str],
    *,
    master: SymbolMaster,
    stale: bool = False,
    opts: TrendingOptions | None = None,
) -> list[InputResult]:
    """One :class:`InputResult` per **enabled** input, eligible names only (pure).

    A ``None`` *buzz* (no entry) gives every input ``missing``; a *stale* entry gives
    every input ``stale``: either way they contribute 0 (rule 2).
    """
    out: list[InputResult] = []
    for name in enabled:
        inp = buzz.inputs.get(name) if buzz is not None else None
        if inp is None:
            out.append(InputResult(name=name, type="?", status="missing", error="no data"))
            continue
        if stale:
            out.append(
                InputResult(
                    name=name,
                    type=inp.type,
                    status="stale",
                    error=f"retail_buzz entry of {buzz.as_of if buzz else '?'} is stale",
                    urls=list(inp.urls),
                    count=len(inp.rows),
                    fetched_at=inp.fetched_at,
                )
            )
            continue
        if inp.status != "ok":
            out.append(_base(name, inp))
            continue
        o = opts or TrendingOptions()
        res = (
            apewisdom_scores(name, inp, scoring=o.scoring, velocity=o.velocity)
            if inp.type == "apewisdom"
            else stocktwits_scores(name, inp)
        )
        for sym in list(res.raw):
            why = eligible(sym, master)
            if why is not None:
                res.dropped[sym] = why
                res.raw.pop(sym)
                res.detail.pop(sym, None)
        if not res.raw:
            res.status = "empty"
        out.append(res)
    return out


class TrendingRow(BaseModel):
    """One ranked name: its input scores, trend score and outcome."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    scores: dict[str, float] = Field(default_factory=dict)  # input name -> (0, 1]
    details: dict[str, str] = Field(default_factory=dict)
    trend_score: float
    n_inputs: int
    excluded: str | None = None  # core | momentum | discovery | market_reference | leveraged
    screen_passed: bool | None = None
    screen_detail: str = ""
    admitted: bool = False
    rank: int | None = None  # rank in the tier (admitted only)

    def reason(self, order: Sequence[str]) -> str:
        """``reddit #3 · stocktwits #5 · score 0.84`` in input order."""
        parts = [self.details[n] for n in order if n in self.details]
        return " · ".join([*parts, f"score {self.trend_score:.2f}"])


def rank_trending(
    inputs: Sequence[InputResult],
    *,
    enabled_count: int,
    exclude: Mapping[str, str],
    min_inputs_first: int,
) -> tuple[list[TrendingRow], list[TrendingRow]]:
    """``(ranked, excluded)`` (pure).

    ``ranked`` = names in >= *min_inputs_first* inputs first, then the rest, each
    group by ``trend_score`` desc then ticker. ``trend_score`` = sum of input scores /
    *enabled_count* (never renormalised).
    """
    per: dict[str, dict[str, float]] = {}
    details: dict[str, dict[str, str]] = {}
    for res in inputs:
        for sym, score in res.scores().items():
            per.setdefault(sym, {})[res.name] = score
            if sym in res.detail:
                details.setdefault(sym, {})[res.name] = res.detail[sym]
    denom = max(enabled_count, 1)
    rows = [
        TrendingRow(
            ticker=sym,
            scores=s,
            details=details.get(sym, {}),
            trend_score=round(sum(s.values()) / denom, 6),
            n_inputs=len(s),
            excluded=exclude.get(sym),
        )
        for sym, s in per.items()
    ]
    rows.sort(key=lambda r: (r.n_inputs < min_inputs_first, -r.trend_score, r.ticker))
    return [r for r in rows if not r.excluded], [r for r in rows if r.excluded]


def screen_pool(
    ranked: Sequence[TrendingRow],
    *,
    pool: int,
    size: int,
    screen: Callable[[str], ScreenResult] | None,
) -> list[TrendingRow]:
    """Screen the first *pool* names in order; the first *size* passes are admitted.

    *screen* ``None`` = no screen (dry runs): the first *size* are admitted unscreened.
    """
    out: list[TrendingRow] = []
    admitted = 0
    for row in ranked[:pool]:
        upd: dict[str, Any] = {}
        ok = True
        if screen is not None and admitted < size:
            res = screen(row.ticker)
            upd |= {"screen_passed": res.passed, "screen_detail": res.detail()}
            ok = res.passed
        if ok and admitted < size:
            admitted += 1
            upd |= {"admitted": True, "rank": admitted}
        out.append(row.model_copy(update=upd))
    return out


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


@dataclass
class TrendingResult:
    """Everything one run computed (the payload, the table, the journal rows)."""

    as_of: _dt.date
    inputs: list[InputResult]
    order: list[str]  # enabled input names, config order
    pool: list[TrendingRow]
    ranked: list[TrendingRow]
    excluded: list[TrendingRow]
    size: int
    pool_size: int
    screened: bool
    buzz_as_of: str | None = None
    scoring: str = "rank_gain"
    # E14.5: symbol -> (velocity, mentions, mentions_24h_ago) from the Reddit input
    velocity: dict[str, Velocity] = field(default_factory=dict)
    buzz: RetailBuzzPayload | None = None  # E14.5: the entry ranked (fastest risers)
    # D64 (E14.7): today's exclusions (ticker -> why) and the symbol master, so the
    # carry-over re-checks names kept from the previous run.
    exclude: dict[str, str] = field(default_factory=dict)
    master: SymbolMaster | None = None

    def carry_excluded(self, sym: str) -> str | None:
        """D64: why a name carried from the previous run is dropped today: the market
        reference or a leveraged / inverse fund (higher tiers are deduped by the
        resolver; carried names are never re-screened)."""
        why = self.exclude.get(sym)
        if why in ("market_reference", EXCLUDED_LEVERAGED):
            return why
        info = self.master.get(sym) if self.master is not None else None
        if info is not None and leveraged(info.name):
            return EXCLUDED_LEVERAGED
        return None

    @property
    def members(self) -> list[TrendingRow]:
        return sorted((r for r in self.pool if r.admitted), key=lambda r: r.rank or 0)

    @property
    def tickers(self) -> list[str]:
        return [r.ticker for r in self.members]

    @property
    def failed_inputs(self) -> dict[str, str]:
        return {i.name: f"{i.status}: {i.error or 'no names'}" for i in self.inputs if not i.live}

    @property
    def leveraged(self) -> list[TrendingRow]:
        return [r for r in self.excluded if r.excluded == EXCLUDED_LEVERAGED]

    def digest(self) -> str:
        return hashlib.sha256(
            "|".join(f"{i.name}:{i.digest or i.count}" for i in self.inputs).encode()
        ).hexdigest()


def latest_buzz(
    conn: sqlite3.Connection, *, now: _dt.datetime, max_age: _dt.timedelta
) -> tuple[RetailBuzzPayload | None, bool]:
    """The newest ``retail_buzz`` entry written at or before *now* and whether it is
    stale (``valid_from`` older than *max_age*). ``(None, False)`` when none exists."""
    from arc.context.kinds import RetailBuzzPayload
    from arc.context.ttl import from_db, to_db

    try:
        row = conn.execute(
            "SELECT payload, valid_from FROM context_entries WHERE kind = 'retail_buzz' "
            "AND subject = 'all' AND valid_from <= ? ORDER BY valid_from DESC, rowid DESC "
            "LIMIT 1",
            (to_db(now),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, False
    if row is None:
        return None, False
    payload = RetailBuzzPayload.model_validate_json(row[0])
    return payload, now - from_db(row[1]) > max_age


def run_trending(
    buzz: RetailBuzzPayload | None,
    *,
    enabled: Sequence[str],
    stale: bool,
    opts: TrendingOptions,
    now: _dt.datetime,
    master: SymbolMaster | None,
    size: int,
    exclude: Mapping[str, str],
    screen: Callable[[str], ScreenResult] | None,
) -> TrendingResult:
    """Score, rank, exclude, screen. Raises :class:`TrendingError` when the tier
    cannot be built (no symbol master, no enabled input, or 0 live inputs)."""
    from arc.utils.calendar import ET

    if master is None:
        msg = "symbol master unavailable (run `arc universe refresh`); trending fails closed"
        raise TrendingError(msg)
    if not enabled:
        msg = "no retail_buzz input enabled"
        raise TrendingError(msg)
    inputs = score_inputs(buzz, enabled, master=master, stale=stale, opts=opts)
    if not any(i.live for i in inputs):
        detail = "; ".join(f"{i.name}: {i.status} ({i.error or 'no names'})" for i in inputs)
        msg = f"0 live retail_buzz inputs: {detail}"
        raise TrendingError(msg)
    excl = dict(exclude)
    for i in inputs:
        for sym in i.raw:
            info = master.get(sym)
            if sym not in excl and info is not None and leveraged(info.name):
                excl[sym] = EXCLUDED_LEVERAGED
    ranked, excluded = rank_trending(
        inputs, enabled_count=len(enabled), exclude=excl, min_inputs_first=opts.min_inputs_first
    )
    pool = screen_pool(ranked, pool=opts.pool, size=size, screen=screen)
    log.info(
        "universe.trending.ranked",
        ranked=len(ranked),
        both=sum(1 for r in ranked if r.n_inputs >= opts.min_inputs_first),
        excluded={r.ticker: r.excluded for r in excluded},
    )
    return TrendingResult(
        as_of=now.astimezone(ET).date(),
        inputs=inputs,
        order=list(enabled),
        pool=pool,
        ranked=ranked,
        excluded=excluded,
        size=size,
        pool_size=opts.pool,
        screened=screen is not None,
        buzz_as_of=buzz.as_of if buzz is not None else None,
        scoring=opts.scoring,
        velocity=buzz_velocities(buzz, opts.velocity),
        buzz=buzz,
        exclude=excl,
        master=master,
    )


def build_payload(res: TrendingResult, *, now: _dt.datetime) -> UniverseTierPayload:
    """The ``universe_tier`` (subject ``trending``) entry for *res* (this run only;
    D64: each member's ``score_today`` / ``score`` = its trend score, ``runs`` = today;
    :func:`arc.universe.carryover.apply_carryover` merges the previous run)."""
    from arc.universe.tiers import Tier, TierMember, UniverseTierPayload

    members = [
        TierMember(
            ticker=r.ticker,
            tier=Tier.TRENDING,
            rank=r.rank or i,
            source="+".join(n for n in res.order if n in r.scores),
            reason=r.reason(res.order),
            as_of=res.as_of,
            inputs=r.n_inputs,
            score=round(r.trend_score, 4),
            score_today=round(r.trend_score, 4),
            runs=[res.as_of],
        )
        for i, r in enumerate(res.members, 1)
    ]
    return UniverseTierPayload(
        tier=Tier.TRENDING,
        members=members,
        fetched_at=now,
        source="retail_buzz:" + "+".join(i.name for i in res.inputs if i.live),
        source_as_of=res.as_of,
        digest=res.digest(),
        url=" ".join(u for i in res.inputs for u in i.urls),
        # an enabled input contributed nothing today: the list is built on fewer inputs
        partial=bool(res.failed_inputs),
    )


def notice_line(
    res: TrendingResult,
    previous: Sequence[str] | None,
    *,
    tickers: Sequence[str] | None = None,
    carried: Sequence[str] = (),
) -> str:
    """``Trending tier: 25 names, 9 in both (+SPCX −RIVN) · inputs reddit, stocktwits``.

    D64: *tickers* = the written (merged) list, compared with *previous*; *carried* =
    the names kept from the previous run (``· 3 carried``).
    """
    cur_list = list(tickers) if tickers is not None else res.tickers
    if previous is None:
        change = "first list"
    else:
        prev, cur = set(previous), set(cur_list)
        moves = [f"+{t}" for t in cur_list if t not in prev] + [
            f"\u2212{t}" for t in previous if t not in cur
        ]
        change = " ".join(moves[:12]) + (" …" if len(moves) > 12 else "") if moves else "no change"
    both = sum(1 for r in res.members if r.n_inputs >= 2)
    live = [i.name for i in res.inputs if i.live]
    parts = [
        f"Trending tier: {len(cur_list)} names, {both} in both inputs ({change})",
        f"inputs {', '.join(live)}",
    ]
    if carried:
        parts.append(f"{len(carried)} carried")
    if res.failed_inputs:
        parts.append("no data: " + ", ".join(res.failed_inputs))
    fails = sum(1 for r in res.pool if r.screen_passed is False)
    if fails:
        parts.append(f"{fails} failed the screen")
    return " · ".join(parts)


def previous_members(conn: sqlite3.Connection) -> list[str] | None:
    """Tickers of the latest trending entry written (any status); ``None`` = never."""
    try:
        row = conn.execute(
            "SELECT payload FROM context_entries WHERE kind = 'universe_tier' "
            "AND subject = 'trending' ORDER BY valid_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    from arc.universe.tiers import UniverseTierPayload

    return [m.ticker for m in UniverseTierPayload.model_validate_json(row[0]).members]


def journal_decisions(
    conn: sqlite3.Connection,
    res: TrendingResult,
    *,
    at: _dt.datetime,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> dict[str, int]:
    """One ``candidate``-stage decision per admission / screen fail / leveraged drop.

    Idempotent per ET day, reason code and ticker (a manual re-run journals only new
    outcomes). Returns the number of rows written per reason code.
    """
    from arc.context.ttl import to_db
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore
    from arc.utils.calendar import ET

    day = res.as_of
    start = _dt.datetime(day.year, day.month, day.day, tzinfo=ET)
    codes = (
        ReasonCode.UNIVERSE_TRENDING_ADMITTED,
        ReasonCode.UNIVERSE_TRENDING_SCREEN_FAIL,
        ReasonCode.UNIVERSE_TRENDING_LEVERAGED,
    )
    done = {
        (r[0], r[1])
        for r in conn.execute(
            f"SELECT reason_code, subject FROM decisions WHERE reason_code IN "
            f"({','.join('?' * len(codes))}) AND at >= ? AND at < ?",
            (*(c.value for c in codes), to_db(start), to_db(start + _dt.timedelta(days=1))),
        ).fetchall()
    }
    rows: list[tuple[ReasonCode, Choice, TrendingRow, str]] = []
    for r in res.pool:
        base = f"{r.reason(res.order)}; {r.n_inputs} input" + ("s" if r.n_inputs > 1 else "")
        if r.admitted:
            rows.append(
                (
                    ReasonCode.UNIVERSE_TRENDING_ADMITTED,
                    Choice.SELECTED,
                    r,
                    f"trending #{r.rank}: {base}",
                )
            )
        elif r.screen_passed is False:
            rows.append(
                (
                    ReasonCode.UNIVERSE_TRENDING_SCREEN_FAIL,
                    Choice.REJECTED,
                    r,
                    f"{base}; screen: {r.screen_detail}",
                )
            )
    for r in res.leveraged:
        rows.append(
            (
                ReasonCode.UNIVERSE_TRENDING_LEVERAGED,
                Choice.REJECTED,
                r,
                f"leveraged/inverse fund ({r.reason(res.order)})",
            )
        )
    store = JournalStore(conn)
    counts = {c.value: 0 for c in codes}
    with conn:
        for code, choice, r, text in rows:
            if (code.value, r.ticker) in done:
                continue
            store.record(
                persona=JournalPersona.SYSTEM,
                stage=Stage.CANDIDATE,
                subject=r.ticker,
                choice=choice,
                reason_code=code,
                reason_text=text[:500],
                confidence=min(r.trend_score, 1.0),
                at=at,
                run_id=run_id,
                chain_run_id=chain_run_id,
                payload={
                    "tier": "trending",
                    "as_of": day.isoformat(),
                    "scores": r.scores,
                    "trend_score": r.trend_score,
                    "inputs": r.n_inputs,
                    "rank": r.rank,
                    "screen": r.screen_detail or None,
                },
            )
            counts[code.value] += 1
    return counts


def table(res: TrendingResult, *, limit: int | None = None) -> list[dict[str, Any]]:
    """The per-input scores table (pool first, in rank order), JSON-friendly."""
    rows = res.pool if limit is None else res.pool[:limit]
    return [
        {
            "ticker": r.ticker,
            **{n: r.scores.get(n) for n in res.order},
            "trend_score": r.trend_score,
            "inputs": r.n_inputs,
            "screen": ("-" if r.screen_passed is None else ("pass" if r.screen_passed else "fail")),
            "screen_detail": r.screen_detail,
            "admitted": r.admitted,
            "rank": r.rank,
            "reason": r.reason(res.order),
        }
        for r in rows
    ]


def normalise_exclusions(exclude: Mapping[str, str], aliases: Mapping[str, str]) -> dict[str, str]:
    """*exclude* plus every share-class alias of an excluded name (GOOG for GOOGL)."""
    out = {normalize_symbol(k): v for k, v in exclude.items()}
    for alias, target in aliases.items():
        t = normalize_symbol(target)
        if t in out:
            out.setdefault(normalize_symbol(alias), out[t])
    return out
