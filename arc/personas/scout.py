"""E13.7 (D56): the Scout persona, a daily read of the slow feed.

Once per trading day (06:00 ET) the Scout reads the two YouTube categories
(``youtube_macro`` and ``youtube_micro`` channel briefs, equal budget) plus
``options_slow`` (``options_daily``, ``vx_curve``, ``vol_term``) plus ``retail_buzz``
(E13.20, D58: the top Reddit + Stocktwits names as context only; the trending tier is
ranked by code and the Scout never adds or removes a name there) and replies with a
:class:`~arc.personas.schemas.ScoutOutput`. Code then validates the reply and decides
everything that leaves the persona:

- ``origins`` must be YouTube channel ids (``youtube:<slug>``) of this run's briefs;
  a call citing anything else is dropped (``origin_unknown``).
- the **discovery tier** comes only from the YouTube ticker calls (owner decision
  1): ``discovery`` ⊆ ``ticker_calls``; core / momentum names, ETFs and the market
  reference are dropped, so are calls below ``universe_floor_discovery`` and names
  failing the ``loose`` liquidity screen; the rest is cut to
  ``funnel.scout.max_discovery``.
- every ticker call at or above its tier's floor becomes a ``candidate``
  (``feed=scout``) for Research.

This module is pure (prompt building and validation); the routine handler
(:func:`arc.routines.handlers.scout_persona`) owns the LLM call and the writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from arc.context.categories import YOUTUBE_CATEGORIES, SourceCategory
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable, Mapping, Sequence

    from arc.context.store import ContextSnapshot
    from arc.context.ttl import Ttl
    from arc.personas.schemas import ScoutOutput, ScoutTickerCall
    from arc.universe.screen import ScreenResult

__all__ = [
    "OPTIONS_SLOW_KINDS",
    "ORIGIN_PREFIX",
    "RETAIL_BUZZ_TOP",
    "SCOUT_CATEGORIES",
    "DiscoveryResult",
    "ScoutInput",
    "build_scout_prompt",
    "category_line",
    "discovery_members",
    "origin_id",
    "retail_buzz_lines",
    "retail_buzz_view",
    "scout_input_from_context",
    "scout_rules",
    "validate_calls",
]

#: ``origins`` are YouTube channel ids: ``youtube:<channel slug>``.
ORIGIN_PREFIX = "youtube:"
#: options_slow kinds the Scout reads (subject ``market``), in prompt order.
OPTIONS_SLOW_KINDS: tuple[str, ...] = ("options_daily", "vx_curve", "vol_term")
#: E13.20 (D58): the Scout's slow-feed categories, in the prompt's category line order.
SCOUT_CATEGORIES: tuple[SourceCategory, ...] = (
    *YOUTUBE_CATEGORIES,
    SourceCategory.OPTIONS_SLOW,
    SourceCategory.RETAIL_BUZZ,
)
#: E13.20: names listed in the prompt's retail-buzz section.
RETAIL_BUZZ_TOP = 15
#: Section headers, in the fixed order the owner set.
SECTIONS: tuple[str, ...] = (
    "Regime",
    "Options sentiment",
    "Themes",
    "Ticker calls",
    "Discovery",
    "Risks",
)

# Screened-out / dropped reasons (journal payload + ``scout_read.screened_out``).
DROP_ORIGIN_UNKNOWN = "origin_unknown"
DROP_NOT_IN_CALLS = "not_in_ticker_calls"
DROP_IN_HIGHER_TIER = "in_higher_tier"
DROP_REFERENCE = "market_reference_or_etf"
DROP_BELOW_FLOOR = "confidence_floor_skipped"
DROP_SCREENED = "screened_out"
DROP_OVER_CAP = "over_max_discovery"
DROP_DUPLICATE = "duplicate"


def origin_id(slug: str) -> str:
    """``youtube:<slug>``: the origin id of a channel's brief."""
    return f"{ORIGIN_PREFIX}{slug}"


class ScoutBrief(BaseModel):
    """One channel brief as the Scout reads it (budgeted)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str
    channel: str
    category: str
    text: str = Field(..., description="JSON of the brief's prompt fields, cut to budget")
    truncated: bool = False


class RetailBuzzName(BaseModel):
    """One ``retail_buzz`` name as the Scout reads it (code-built, context only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    reddit_rank: int | None = None
    reddit_mentions: float | None = None
    stocktwits_rank: int | None = None
    in_trending: bool = Field(False, description="In today's trending tier (code, E13.19)")

    @property
    def n_inputs(self) -> int:
        return int(self.reddit_rank is not None) + int(self.stocktwits_rank is not None)

    def line(self) -> str:
        """``- GME · reddit #1/1,234 mentions · stocktwits #4 · in trending tier y``."""
        if self.reddit_rank is None:
            reddit = "reddit —"
        else:
            m = self.reddit_mentions
            reddit = f"reddit #{self.reddit_rank}" + (
                f"/{m:,.0f} mentions" if m is not None else ""
            )
        st = (
            "stocktwits —"
            if self.stocktwits_rank is None
            else f"stocktwits #{self.stocktwits_rank}"
        )
        flag = "y" if self.in_trending else "n"
        return f"- {self.ticker} · {reddit} · {st} · in trending tier {flag}"


class RetailBuzzView(BaseModel):
    """The fresh ``retail_buzz`` entry, cut to the top :data:`RETAIL_BUZZ_TOP` names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: str
    inputs: dict[str, str] = Field(default_factory=dict, description="input name -> status")
    names: list[RetailBuzzName] = Field(default_factory=list)


class ScoutInput(BaseModel):
    """Everything the Scout prompt is built from (recorded as the prompt inputs)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session: str
    as_of: str
    max_discovery: int
    discovery_floor: float
    budget_chars: int
    presence: dict[str, str] = Field(default_factory=dict, description="category -> line")
    present: dict[str, list[str]] = Field(default_factory=dict)
    missing: dict[str, list[str]] = Field(default_factory=dict)
    configured: dict[str, int] = Field(default_factory=dict)
    briefs: list[ScoutBrief] = Field(default_factory=list)
    options_slow: dict[str, dict[str, Any] | None] = Field(default_factory=dict)
    options_as_of: dict[str, str | None] = Field(default_factory=dict)
    higher_tier: list[str] = Field(default_factory=list, description="core + momentum names")
    # E13.20 (D58): retail_buzz as context only (None = no fresh entry: "no info")
    retail_buzz: RetailBuzzView | None = None

    @property
    def categories_present(self) -> list[str]:
        """The slow-feed categories with fresh input this run, in :data:`SCOUT_CATEGORIES`
        order (a YouTube category needs one fresh brief; options_slow one fresh kind)."""
        fresh = {
            **{c.value: bool(self.present.get(c.value)) for c in YOUTUBE_CATEGORIES},
            SourceCategory.OPTIONS_SLOW.value: any(v for v in self.options_slow.values()),
            SourceCategory.RETAIL_BUZZ.value: self.retail_buzz is not None,
        }
        return [c.value for c in SCOUT_CATEGORIES if fresh[c.value]]

    @property
    def origins(self) -> frozenset[str]:
        return frozenset(b.origin for b in self.briefs)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _fresh(valid_from: _dt.datetime, as_of: _dt.datetime, max_age: Ttl) -> bool:
    """Fresh while the category's ``max_age`` (from ``valid_from``) has not run out."""
    return valid_from <= as_of < max_age.expires_at(valid_from)


def _budget_split(total: int, cats: Mapping[str, int]) -> dict[str, int]:
    """Equal split of *total* chars between the categories, then equally among the
    channels present in each (D56 ``video_budget_split: equal``). A category with no
    brief gives its share to nobody: the budget is a cap, never re-normalised, so one
    category's volume never crowds out the other's."""
    per_cat = total // max(1, len(cats))
    return {c: (per_cat // n if n else 0) for c, n in cats.items()}


def scout_input_from_context(
    snapshot: ContextSnapshot,
    *,
    channels: Sequence[Mapping[str, str]],
    budget_chars: int,
    max_age: Mapping[str, Ttl],
    max_discovery: int,
    discovery_floor: float,
    higher_tier: Sequence[str] = (),
    trending: Sequence[str] = (),
    now: _dt.datetime | None = None,
) -> ScoutInput:
    """Build the Scout's input from the snapshot (pure).

    *channels* is the ``youtube.briefs`` config (``[{slug, label, category}]``) so a
    missing channel is named. *max_age* maps each category (``youtube_macro``,
    ``youtube_micro``, ``options_slow``, ``retail_buzz``) to its freshness window: an
    older entry is "no fresh info", never read. *trending* = today's trending tier
    (E13.20: marks ``in trending tier y`` in the retail-buzz section).
    """
    from arc.ingest.channels.daily import brief_ages, brief_presence_line, prompt_brief

    as_of = (now or snapshot.as_of).astimezone(ET)
    labels = {c["slug"]: c.get("label") or c["slug"] for c in channels}
    cat_of = {c["slug"]: str(c.get("category") or "") for c in channels}
    fresh_briefs: dict[str, dict[str, Any]] = {}
    for e in snapshot.of_kind("channel_brief"):
        slug = str(e.payload.get("channel_slug"))
        cat = cat_of.get(slug)
        if cat is None or cat not in {c.value for c in YOUTUBE_CATEGORIES}:
            continue
        if not _fresh(e.valid_from, as_of, max_age[cat]):
            continue
        prev = fresh_briefs.get(slug)
        if prev is None or str(e.payload.get("published_at")) > str(prev.get("published_at")):
            fresh_briefs[slug] = e.payload
    presence: dict[str, str] = {}
    present: dict[str, list[str]] = {}
    missing: dict[str, list[str]] = {}
    configured: dict[str, int] = {}
    for cat in YOUTUBE_CATEGORIES:
        chs = [c for c in channels if c.get("category") == cat.value]
        configured[cat.value] = len(chs)
        presence[cat.value] = brief_presence_line(
            list(fresh_briefs), chs, cat, ages=brief_ages(fresh_briefs.values(), as_of)
        )
        present[cat.value] = [c["slug"] for c in chs if c["slug"] in fresh_briefs]
        missing[cat.value] = [labels[c["slug"]] for c in chs if c["slug"] not in fresh_briefs]
    per_channel = _budget_split(budget_chars, {c: len(p) for c, p in present.items()})
    briefs: list[ScoutBrief] = []
    for cat in YOUTUBE_CATEGORIES:
        for slug in present[cat.value]:
            text = json.dumps(
                prompt_brief(fresh_briefs[slug], labels[slug]), sort_keys=True, default=str
            )
            cap = per_channel[cat.value]
            briefs.append(
                ScoutBrief(
                    origin=origin_id(slug),
                    channel=labels[slug],
                    category=cat.value,
                    text=text[:cap],
                    truncated=len(text) > cap,
                )
            )
    options: dict[str, dict[str, Any] | None] = {}
    options_as_of: dict[str, str | None] = {}
    for kind in OPTIONS_SLOW_KINDS:
        e = snapshot.latest(kind, "market")
        if e is None or not _fresh(e.valid_from, as_of, max_age["options_slow"]):
            options[kind], options_as_of[kind] = None, None
            continue
        options[kind] = dict(e.payload)
        options_as_of[kind] = str(e.payload.get("as_of") or "") or None
    buzz_age = max_age.get(SourceCategory.RETAIL_BUZZ.value)
    buzz_entry = snapshot.latest("retail_buzz", "all")
    buzz = (
        retail_buzz_view(buzz_entry.payload, trending=trending)
        if buzz_entry is not None
        and buzz_age is not None
        and _fresh(buzz_entry.valid_from, as_of, buzz_age)
        else None
    )
    return ScoutInput(
        session=as_of.date().isoformat(),
        as_of=as_of.isoformat(),
        max_discovery=max_discovery,
        discovery_floor=discovery_floor,
        budget_chars=budget_chars,
        presence=presence,
        present=present,
        missing=missing,
        configured=configured,
        briefs=briefs,
        options_slow=options,
        options_as_of=options_as_of,
        higher_tier=sorted(higher_tier),
        retail_buzz=buzz,
    )


def retail_buzz_view(
    payload: Mapping[str, Any], *, trending: Sequence[str] = (), top: int = RETAIL_BUZZ_TOP
) -> RetailBuzzView:
    """The top *top* ``retail_buzz`` names for the Scout's prompt (pure, code-built).

    Ordered like the trending ranker (:func:`arc.universe.trending.rank_trending`, no
    eligibility or tier exclusion here): names both inputs list first, then by the
    rank-normalised score summed over the inputs, then ticker. ``in_trending`` is
    ``True`` only for names in today's trending tier (*trending*); the Scout reads this
    as context and never adds or removes a trending name.
    """
    from arc.context.kinds import RetailBuzzPayload
    from arc.universe.trending import apewisdom_scores, rank_trending, stocktwits_scores

    buzz = RetailBuzzPayload.model_validate(payload)
    results = []
    reddit: dict[str, tuple[int, float | None]] = {}
    stocktwits: dict[str, int] = {}
    for name, inp in buzz.inputs.items():
        if inp.status != "ok" or not inp.rows:
            continue
        if inp.type == "apewisdom":
            results.append(apewisdom_scores(name, inp))
            for r in inp.rows:
                reddit.setdefault(r.symbol, (r.rank or r.position, r.mentions))
        else:
            res = stocktwits_scores(name, inp)
            results.append(res)
            for r in inp.rows:
                if r.symbol in res.raw:
                    stocktwits.setdefault(r.symbol, r.rank or r.position)
    ranked, _ = rank_trending(
        results, enabled_count=max(len(buzz.inputs), 1), exclude={}, min_inputs_first=2
    )
    tier = {t.strip().upper() for t in trending}
    names = [
        RetailBuzzName(
            ticker=r.ticker,
            reddit_rank=reddit[r.ticker][0] if r.ticker in reddit else None,
            reddit_mentions=reddit[r.ticker][1] if r.ticker in reddit else None,
            stocktwits_rank=stocktwits.get(r.ticker),
            in_trending=r.ticker in tier,
        )
        for r in ranked[: max(0, top)]
    ]
    return RetailBuzzView(
        as_of=buzz.as_of,
        inputs={n: i.status for n, i in buzz.inputs.items()},
        names=names,
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def options_slow_lines(options: Mapping[str, Mapping[str, Any] | None]) -> list[str]:
    """Compact, code-rendered options_slow lines (no prose; "no fresh info" if absent)."""
    lines: list[str] = []
    od = options.get("options_daily")
    if od:
        ratios = ", ".join(
            f"{r['segment']} {float(r['ratio']):.2f}" for r in od.get("ratios") or []
        )
        lines.append(f"- Cboe put/call ({od.get('as_of')}): {ratios}")
        for oi in od.get("open_interest") or []:
            lines.append(
                f"  - OI {oi['product']}: calls {oi['call_oi']:,} · puts {oi['put_oi']:,}"
                + (f" · volume {oi['volume']:,}" if oi.get("volume") is not None else "")
            )
    else:
        lines.append("- Cboe put/call: no fresh info")
    vx = options.get("vx_curve")
    if vx:
        lines.append(
            f"- VX futures ({vx.get('as_of')}): front {vx['front']:.2f} · second "
            f"{vx['second']:.2f} · back {vx['back']:.2f} · slope 1→2 "
            f"{vx['slope_1_2_pct']:+.1f}% · {vx['shape']}"
        )
    else:
        lines.append("- VX futures curve: no fresh info")
    vt = options.get("vol_term")
    if vt:
        parts = [f"{k.upper()} {vt[k]:.2f}" for k in ("vix9d", "vix", "vix3m", "vvix") if vt.get(k)]
        lines.append(f"- VIX complex ({vt.get('as_of')}): {' · '.join(parts)} · {vt['structure']}")
    else:
        lines.append("- VIX complex: no fresh info")
    return lines


def category_line(inp: ScoutInput) -> str:
    """``Categories: 3/4 present (youtube_macro, youtube_micro, options_slow; no fresh
    info: retail_buzz)``: code-counted, in :data:`SCOUT_CATEGORIES` order."""
    present = inp.categories_present
    absent = [c.value for c in SCOUT_CATEGORIES if c.value not in present]
    detail = ", ".join(present) or "none"
    if absent:
        detail += f"; no fresh info: {', '.join(absent)}"
    return f"Categories: {len(present)}/{len(SCOUT_CATEGORIES)} present ({detail})"


def retail_buzz_lines(view: RetailBuzzView | None) -> list[str]:
    """E13.20 (D58): the code-built retail-buzz section (``no info`` when not fresh).

    Context only: the trending tier is ranked by code (E13.19); the Scout cannot add or
    remove a trending name and retail buzz alone never makes a discovery name.
    """
    if view is None:
        return ["## Retail buzz (retail_buzz, Reddit + Stocktwits)", "- no info"]
    inputs = ", ".join(f"{n} {s}" for n, s in view.inputs.items())
    lines = [
        f"## Retail buzz (as of {view.as_of}; Reddit + Stocktwits: crowd attention, context only)",
        f"Inputs: {inputs}. Top {len(view.names)} by attention, names in both inputs first.",
        "`in trending tier` is set by code; you cannot add or remove trending-tier names.",
        "Buzz is a weak signal: never cite it alone as a thesis or a discovery reason.",
    ]
    lines += [n.line() for n in view.names] or ["- no names"]
    return lines


def build_scout_prompt(inp: ScoutInput) -> str:
    """The Scout's prompt (sections in the fixed order; schema appended by the caller)."""
    lines = [
        "You are the Scout, the daily slow-feed reader of Project Arc (options trading).",
        f"Session: {inp.session} (read at {inp.as_of}). You read the YouTube channel briefs",
        "and the Cboe end-of-day options statistics below and write the morning read. The",
        "retail buzz (Reddit + Stocktwits) is context only: corroborate it with a brief or",
        "the options data before it shapes a call.",
        "",
        "Answer in these sections, in this order:",
        "1. Regime: the market regime the inputs describe (trend, volatility, breadth);",
        "   at most 600 characters.",
        "2. Options sentiment: what put/call, open interest, the VX curve and the VIX",
        "   complex say about positioning, in at most 600 characters. Say 'no fresh info'",
        "   for an absent input.",
        "3. Themes: up to 8 one-line themes shared across channels.",
        "4. Ticker calls: single-name ideas the channels made, each citing the channel ids",
        "   (origins) that made it. Only names a brief below actually discusses.",
        f"5. Discovery: up to {inp.max_discovery} of your ticker-call tickers, best first, that",
        "   are NOT already in the core or momentum tiers (listed below) and are not ETFs or",
        f"   index products. Only calls with confidence >= {inp.discovery_floor:.2f} qualify.",
        "6. Risks: up to 6 risks to the read.",
        "",
        category_line(inp),
        "",
        "## YouTube briefs (equal budget per category, then per channel present)",
    ]
    for cat in YOUTUBE_CATEGORIES:
        lines.append(inp.presence.get(cat.value, ""))
        for b in (x for x in inp.briefs if x.category == cat.value):
            cut = " (cut to budget)" if b.truncated else ""
            lines.append(f"### {b.channel} · origin id `{b.origin}`{cut}")
            lines.append(b.text)
    if not inp.briefs:
        lines.append("No fresh brief today: make no ticker calls and leave discovery empty.")
    lines += [
        "",
        "## Options (options_slow, Cboe end of day)",
        *options_slow_lines(inp.options_slow),
    ]
    lines += ["", *retail_buzz_lines(inp.retail_buzz)]
    lines += [
        "",
        "## Already in a higher tier (never list these in discovery)",
        ", ".join(inp.higher_tier) or "(none)",
        "",
    ]
    return "\n".join(lines)


def scout_rules(inp: ScoutInput) -> list[str]:
    """Hard-constraint lines (appended with the schema; recorded with the inputs)."""
    origins = ", ".join(sorted(inp.origins)) or "(none today)"
    return [
        f"Every ticker call's origins are channel ids from this list only: {origins}.",
        "A call citing any other origin is dropped.",
        "discovery lists only tickers that appear in ticker_calls.",
        f"At most {inp.max_discovery} discovery names; core/momentum names, ETFs and "
        "index products are dropped from discovery.",
        f"A discovery name below confidence {inp.discovery_floor:.2f} is dropped.",
        "Every discovery name must pass a code liquidity screen (loose); failures are dropped.",
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_calls(
    out: ScoutOutput, origins: frozenset[str], *, max_calls: int = 30
) -> tuple[list[ScoutTickerCall], dict[str, str]]:
    """Keep calls whose every origin is one of this run's channel ids (first call per
    ticker wins). Returns ``(calls, dropped)`` with ``dropped`` = ticker -> reason."""
    kept: list[ScoutTickerCall] = []
    dropped: dict[str, str] = {}
    seen: set[str] = set()
    for call in out.ticker_calls:
        if call.ticker in seen:
            dropped.setdefault(call.ticker, DROP_DUPLICATE)
            continue
        bad = [o for o in call.origins if o not in origins]
        if bad:
            dropped[call.ticker] = f"{DROP_ORIGIN_UNKNOWN}: {', '.join(bad)}"
            continue
        seen.add(call.ticker)
        kept.append(call.model_copy(update={"origins": list(dict.fromkeys(call.origins))}))
    return kept[:max_calls], dropped


@dataclass
class DiscoveryResult:
    """The code-screened discovery tier and why each other name is out."""

    members: list[ScoutTickerCall] = field(default_factory=list)
    screened_out: dict[str, str] = field(default_factory=dict)
    #: ticker -> (reason code, detail) for journaling
    journal: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        return [c.ticker for c in self.members]


def discovery_members(
    out: ScoutOutput,
    calls: Sequence[ScoutTickerCall],
    *,
    higher_tier: Mapping[str, str],
    excluded: frozenset[str],
    floor: float,
    max_discovery: int,
    screen: Callable[[str], ScreenResult],
) -> DiscoveryResult:
    """Deterministic discovery rules over the reply's ordered ``discovery`` list.

    *higher_tier* maps core / momentum names to their tier; *excluded* is ETFs plus
    the market reference; *screen* runs the ``loose`` liquidity screen (called only
    for names that passed every cheaper rule, so no market data is spent on them).
    """
    by_ticker = {c.ticker: c for c in calls}
    res = DiscoveryResult()
    for raw in out.discovery:
        sym = raw.strip().upper()
        if sym in res.screened_out or sym in res.tickers:
            continue
        call = by_ticker.get(sym)
        if call is None:
            res.screened_out[sym] = DROP_NOT_IN_CALLS
            continue
        if sym in higher_tier:
            res.screened_out[sym] = f"{DROP_IN_HIGHER_TIER}: {higher_tier[sym]}"
            res.journal.append((sym, DROP_IN_HIGHER_TIER, higher_tier[sym]))
            continue
        if sym in excluded:
            res.screened_out[sym] = DROP_REFERENCE
            continue
        if call.confidence < floor:
            res.screened_out[sym] = f"{DROP_BELOW_FLOOR}: {call.confidence:.2f} < {floor:.2f}"
            res.journal.append((sym, DROP_BELOW_FLOOR, f"{call.confidence:.2f} < {floor:.2f}"))
            continue
        if len(res.members) >= max_discovery:
            res.screened_out[sym] = DROP_OVER_CAP
            continue
        result = screen(sym)
        if not result.passed:
            detail = result.detail()
            res.screened_out[sym] = f"{DROP_SCREENED}: {detail}"
            res.journal.append((sym, DROP_SCREENED, detail))
            continue
        res.members.append(call)
    return res


def youtube_category_values() -> frozenset[str]:
    return frozenset(c.value for c in YOUTUBE_CATEGORIES)


def category_max_ages(routines: Any) -> dict[str, Ttl]:
    """``{youtube_macro, youtube_micro, options_slow, retail_buzz: max_age}`` from the
    effective config."""
    return {c.value: routines.category_spec(c).max_age for c in SCOUT_CATEGORIES}
