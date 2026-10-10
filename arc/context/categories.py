"""Source categories (D47, D49, D56, D58): seven equal-weight categories with a freshness
window each.

Every ingest source belongs to exactly one :class:`SourceCategory`, or is **reference
data** (D56: ``reference: true``; ex-dividend, macro calendar, earnings calendar,
Finnhub kinds, ``iv_daily``), which existing readers (Risk step, gate blackout,
regime) use unchanged and which is never shown as a category. The categories, not
the individual sources, are weighted equally; a source shares its category's
weight with the other sources in it (``weight:`` on a source = its share *inside*
the category). Each category declares a freshness ``max_age``; a source inherits it
unless it sets its own ``max_age:``.

The block lives in ``config/routines.yaml`` (``categories:``) and is the single
place to tune them. A missing block falls back to :data:`DEFAULT_CATEGORIES`.

Pure: no I/O, no clock (callers pass ``now``).
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.context.ttl import Ttl

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Iterable, Mapping

log = structlog.get_logger()

__all__ = [
    "CATEGORY_ALIASES",
    "CATEGORY_ORDER",
    "DEFAULT_CATEGORIES",
    "KIND_CATEGORY",
    "LEGACY_VIDEO",
    "REFERENCE",
    "REFERENCE_KINDS",
    "RESEARCH_CATEGORIES",
    "SCALP_CATEGORIES",
    "YOUTUBE_CATEGORIES",
    "CategorySpec",
    "SourceCategory",
    "age_text",
    "channel_categories",
    "channel_category",
    "earliest_ttl",
    "is_stale",
    "kind_category",
    "normalize_category",
    "parse_category",
    "parse_youtube_category",
]


class SourceCategory(enum.StrEnum):
    """D56 + D58: exactly seven categories, in the fixed display order."""

    MARKET_NEWS = "market_news"
    COMPANY_DATA = "company_data"
    OPTIONS_FAST = "options_fast"
    OPTIONS_SLOW = "options_slow"
    YOUTUBE_MACRO = "youtube_macro"
    YOUTUBE_MICRO = "youtube_micro"
    # D58 (E13.19): Reddit (ApeWisdom) + Stocktwits, one daily pull in the Scout's slow
    # feed; ranked into the trending tier by code (arc.universe.trending).
    RETAIL_BUZZ = "retail_buzz"


CATEGORY_ORDER: tuple[SourceCategory, ...] = tuple(SourceCategory)
#: The categories Research's "Context by category" block lists (D56's six). D58's
#: ``retail_buzz`` feeds the trending tier (code) and the Scout, never Research.
RESEARCH_CATEGORIES: tuple[SourceCategory, ...] = tuple(
    c for c in CATEGORY_ORDER if c is not SourceCategory.RETAIL_BUZZ
)

# Old names, accepted for one release (logged as ``sources.category_alias``) so open
# branches and YAML keep loading: the pre-D47 names, the D47 names D49 renamed and
# the D49 names D56 replaced. ``None`` = no single successor: config refuses the name
# with a pointed message (:data:`REMOVED_CATEGORY_HINTS`) and a stored value reads
# as no category.
CATEGORY_ALIASES: Mapping[str, SourceCategory | None] = {
    "company": SourceCategory.COMPANY_DATA,
    "company_news": SourceCategory.COMPANY_DATA,
    "filings": SourceCategory.COMPANY_DATA,
    "calendar": SourceCategory.COMPANY_DATA,
    "options_data": SourceCategory.OPTIONS_SLOW,  # D56: the daily Cboe snapshots
    "macro_data": None,
    "macro": None,
}

# D56: why a name with no successor was removed (the config error says what to do).
REMOVED_CATEGORY_HINTS: Mapping[str, str] = {
    "macro_data": (
        "was removed (D56): the Fed feed is `category: market_news`; macro_calendar is "
        "reference data (`reference: true`, no category)"
    ),
    "macro": (
        "was removed (D56): the Fed feed is `category: market_news`; macro_calendar is "
        "reference data (`reference: true`, no category)"
    ),
}

# D56: reference data is not a category. Sources declare ``reference: true`` instead
# of ``category:``; their kinds keep their own ``context_ttl`` and their readers
# (Risk step, gate blackout via ``next_earnings()`` on raw_docs source='earnings',
# regime via the ``iv_daily`` table) are unchanged.
REFERENCE = "reference"
REFERENCE_KINDS: frozenset[str] = frozenset(
    {
        "ex_dividend",
        "macro_calendar",
        "earnings_history",
        "insider_activity",
        "analyst_recs",
        "fundamentals",
        # E16.4 (D76): market-health histories + the daily read (derived, no category)
        "index_history",
        "pc_history",
        "market_health",
    }
)

# D47's ``video`` was split in two (D49). It cannot map to one category, so config
# refuses it (each channel declares its own); stored rows resolve it by channel.
LEGACY_VIDEO = "video"

# The two YouTube categories (D49): every ``youtube.briefs`` channel declares one.
YOUTUBE_CATEGORIES: tuple[SourceCategory, ...] = (
    SourceCategory.YOUTUBE_MACRO,
    SourceCategory.YOUTUBE_MICRO,
)

# Categories whose docs share the Scalp's ``scalp_doc_budget`` (raw docs), split
# equally between them (D56: exactly these two). Options data and YouTube reach the
# personas as typed context only (D45, D47, D49, D56).
SCALP_CATEGORIES: frozenset[SourceCategory] = frozenset(
    {SourceCategory.MARKET_NEWS, SourceCategory.COMPANY_DATA}
)

# Typed context kinds (never raw docs) and the category they report under.
# ``channel_brief`` is not here: a brief's category is its channel's (see
# :func:`channel_category`), since D49 splits YouTube in two. Reference kinds
# (:data:`REFERENCE_KINDS`) have no category (D56).
KIND_CATEGORY: Mapping[str, SourceCategory] = {
    "vol_term": SourceCategory.OPTIONS_SLOW,
    "options_daily": SourceCategory.OPTIONS_SLOW,  # E13.5 (D56)
    "vx_curve": SourceCategory.OPTIONS_SLOW,
    # E13.6 (D56): options_fast (Scalp, 30-min RTH Cboe delayed quotes + symbol_data)
    "index_vols": SourceCategory.OPTIONS_FAST,
    "chain_snapshot": SourceCategory.OPTIONS_FAST,
    "exchange_volume": SourceCategory.OPTIONS_FAST,
    "market_movers": SourceCategory.OPTIONS_FAST,  # E14.3 (D60): Scalp context only
    # E13.19 (D58): Reddit + Stocktwits raw rows, daily (subject all)
    "retail_buzz": SourceCategory.RETAIL_BUZZ,
}


def kind_category(kind: str) -> str | None:
    """A typed kind's category value, ``reference`` for reference data, else ``None``."""
    if kind in KIND_CATEGORY:
        return KIND_CATEGORY[kind].value
    return REFERENCE if kind in REFERENCE_KINDS else None


def _video_refused(where: str) -> ValueError:
    names = " | ".join(c.value for c in YOUTUBE_CATEGORIES)
    at = f" ({where})" if where else ""
    msg = (
        f"source category 'video'{at} was split in two (D49): remove it and set "
        f"`category: {names}` on each youtube.briefs channel"
    )
    return ValueError(msg)


def _removed(text: str, where: str) -> ValueError:
    at = f" ({where})" if where else ""
    return ValueError(f"source category {text!r}{at} {REMOVED_CATEGORY_HINTS[text]}")


def parse_category(raw: Any, *, where: str = "") -> SourceCategory:
    """Strict config parse: a D56 name or a logged old alias; anything else raises.

    ``video`` raises with a pointer to the per-channel ``category:`` (D49);
    ``macro_data`` / ``macro`` raise with a pointer to ``market_news`` and
    ``reference: true`` (D56).
    """
    if isinstance(raw, SourceCategory):
        return raw
    text = str(raw).strip().lower()
    try:
        return SourceCategory(text)
    except ValueError:
        pass
    if text in CATEGORY_ALIASES:
        new = CATEGORY_ALIASES[text]
        if new is None:
            raise _removed(text, where)
        log.warning("sources.category_alias", old=text, new=new.value, where=where)
        return new
    if text == LEGACY_VIDEO:
        raise _video_refused(where)
    names = " | ".join(c.value for c in SourceCategory)
    msg = f"unknown source category {raw!r}{f' ({where})' if where else ''}; expected {names}"
    raise ValueError(msg)


def parse_youtube_category(raw: Any, *, where: str = "") -> SourceCategory:
    """A channel's ``category:``: exactly one of the YouTube categories (D49)."""
    if raw is None or str(raw).strip() == "":
        names = " | ".join(c.value for c in YOUTUBE_CATEGORIES)
        msg = f"{where or 'channel'}: missing `category:` ({names}); D49"
        raise ValueError(msg)
    cat = parse_category(raw, where=where)
    if cat not in YOUTUBE_CATEGORIES:
        names = " | ".join(c.value for c in YOUTUBE_CATEGORIES)
        msg = f"{where or 'channel'}: category {cat.value!r} is not a YouTube category ({names})"
        raise ValueError(msg)
    return cat


def _slug(raw: Any) -> str:
    return str(raw or "").strip().removeprefix("youtube.")


def channel_categories(channels: Iterable[Mapping[str, Any]]) -> dict[str, SourceCategory]:
    """``slug -> category`` of configured channels (entries without one are skipped)."""
    out: dict[str, SourceCategory] = {}
    for ch in channels:
        cat = normalize_category(ch.get("category"))
        if cat is not None and ch.get("slug"):
            out[_slug(ch["slug"])] = cat
    return out


def channel_category(slug: Any, channels: Iterable[Mapping[str, Any]]) -> SourceCategory | None:
    """D49: a ``channel_brief``'s category = its channel's config entry (``None`` if absent).

    *slug* may carry the ``youtube.`` source-key prefix; *channels* are the
    ``youtube.briefs`` ``channels:`` entries (``{slug, category, ...}``).
    """
    return channel_categories(channels).get(_slug(slug))


def normalize_category(
    raw: Any,
    *,
    channel: Any = None,
    channels: Iterable[Mapping[str, Any]] = (),
) -> SourceCategory | None:
    """Lenient read of a *stored* category value (old rows); never logs or raises.

    Old names map through :data:`CATEGORY_ALIASES` (a removed name such as
    ``macro_data`` reads as ``None``); a stored ``video`` resolves by *channel* (slug
    or ``youtube.<slug>``) against the configured *channels*, else ``None``.
    """
    text = str(raw or "").strip().lower()
    if text in SourceCategory.__members__.values():
        return SourceCategory(text)
    if text == LEGACY_VIDEO:
        return channel_category(channel, channels) if channel else None
    return CATEGORY_ALIASES.get(text)


class CategorySpec(BaseModel):
    """One ``categories.<name>`` entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    weight: Annotated[float, Field(ge=0, le=5)] = 1.0
    max_age: Ttl
    label: str = Field(..., min_length=1, max_length=40)

    @field_validator("max_age", mode="before")
    @classmethod
    def _ttl(cls, v: Any) -> Any:
        return Ttl.model_validate(v)


def _spec(max_age: str, label: str) -> CategorySpec:
    return CategorySpec.model_validate({"max_age": max_age, "label": label})


DEFAULT_CATEGORIES: Mapping[SourceCategory, CategorySpec] = {
    SourceCategory.MARKET_NEWS: _spec("6h", "Market news"),
    SourceCategory.COMPANY_DATA: _spec("12h", "Company data"),
    SourceCategory.OPTIONS_FAST: _spec("30m", "Options fast"),
    SourceCategory.OPTIONS_SLOW: _spec("24h", "Options slow"),
    SourceCategory.YOUTUBE_MACRO: _spec("48h", "YouTube macro"),  # D60: was 24h
    SourceCategory.YOUTUBE_MICRO: _spec("48h", "YouTube micro"),  # D60: was 24h
    SourceCategory.RETAIL_BUZZ: _spec("24h", "Retail buzz"),  # D58
}


def is_stale(published: _dt.datetime, max_age: Ttl, now: _dt.datetime) -> bool:
    """True when an item published at *published* is past *max_age* at *now*."""
    return max_age.expires_at(published) <= now


def age_text(delta: _dt.timedelta) -> str:
    """``22m`` / ``5h`` / ``2d``: a compact age for code-built freshness lines."""
    secs = max(0, int(delta.total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 2 * 86_400:
        return f"{secs // 3600}h"
    return f"{secs // 86_400}d"


def earliest_ttl(ttls: Iterable[Ttl], at: _dt.datetime) -> Ttl | None:
    """The TTL that expires first for an entry valid from *at* (``None`` if none given)."""
    best: Ttl | None = None
    for t in ttls:
        if best is None or t.expires_at(at) < best.expires_at(at):
            best = t
    return best
