"""Source categories (D47, D49): six equal-weight categories with a freshness window each.

Every ingest source belongs to exactly one :class:`SourceCategory`. The categories,
not the individual sources, are weighted equally; a source shares its category's
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
    "SCALP_CATEGORIES",
    "YOUTUBE_CATEGORIES",
    "CategorySpec",
    "SourceCategory",
    "age_text",
    "channel_categories",
    "channel_category",
    "earliest_ttl",
    "is_stale",
    "normalize_category",
    "parse_category",
    "parse_youtube_category",
]


class SourceCategory(enum.StrEnum):
    """D49: exactly six categories, in the fixed display order."""

    MARKET_NEWS = "market_news"
    COMPANY_DATA = "company_data"
    MACRO_DATA = "macro_data"
    OPTIONS_DATA = "options_data"
    YOUTUBE_MACRO = "youtube_macro"
    YOUTUBE_MICRO = "youtube_micro"


CATEGORY_ORDER: tuple[SourceCategory, ...] = tuple(SourceCategory)

# Old names, accepted for one release (logged as ``sources.category_alias``) so open
# branches and YAML keep loading: the pre-D47 names and the D47 names D49 renamed.
CATEGORY_ALIASES: Mapping[str, SourceCategory] = {
    "company": SourceCategory.COMPANY_DATA,
    "macro": SourceCategory.MACRO_DATA,
    "company_news": SourceCategory.COMPANY_DATA,
    "filings": SourceCategory.COMPANY_DATA,
    "calendar": SourceCategory.COMPANY_DATA,
}

# D47's ``video`` was split in two (D49). It cannot map to one category, so config
# refuses it (each channel declares its own); stored rows resolve it by channel.
LEGACY_VIDEO = "video"

# The two YouTube categories (D49): every ``youtube.briefs`` channel declares one.
YOUTUBE_CATEGORIES: tuple[SourceCategory, ...] = (
    SourceCategory.YOUTUBE_MACRO,
    SourceCategory.YOUTUBE_MICRO,
)

# Categories whose docs share the Scalp's ``scalp_doc_budget`` (raw docs). Options
# data and YouTube reach Research as typed context only (D45, D47, D49).
SCALP_CATEGORIES: frozenset[SourceCategory] = frozenset(
    {SourceCategory.MARKET_NEWS, SourceCategory.COMPANY_DATA, SourceCategory.MACRO_DATA}
)

# Typed context kinds (never raw docs) and the category they report under.
# ``channel_brief`` is not here: a brief's category is its channel's (see
# :func:`channel_category`), since D49 splits YouTube in two.
KIND_CATEGORY: Mapping[str, SourceCategory] = {
    "vol_term": SourceCategory.OPTIONS_DATA,
    "put_call": SourceCategory.OPTIONS_DATA,
    "unusual_options": SourceCategory.OPTIONS_DATA,
    "ex_dividend": SourceCategory.OPTIONS_DATA,
    "macro_calendar": SourceCategory.MACRO_DATA,
    # D46 Finnhub per-ticker kinds (E4.8).
    "earnings_history": SourceCategory.COMPANY_DATA,
    "insider_activity": SourceCategory.COMPANY_DATA,
    "analyst_recs": SourceCategory.COMPANY_DATA,
    "fundamentals": SourceCategory.COMPANY_DATA,
}


def _video_refused(where: str) -> ValueError:
    names = " | ".join(c.value for c in YOUTUBE_CATEGORIES)
    at = f" ({where})" if where else ""
    msg = (
        f"source category 'video'{at} was split in two (D49): remove it and set "
        f"`category: {names}` on each youtube.briefs channel"
    )
    return ValueError(msg)


def parse_category(raw: Any, *, where: str = "") -> SourceCategory:
    """Strict config parse: a D49 name or a logged old alias; anything else raises.

    ``video`` raises with a pointer to the per-channel ``category:`` (D49).
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

    Old names map through :data:`CATEGORY_ALIASES`; a stored ``video`` resolves by
    *channel* (slug or ``youtube.<slug>``) against the configured *channels*, else
    ``None``.
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
    SourceCategory.COMPANY_DATA: _spec("24h", "Company data"),
    SourceCategory.MACRO_DATA: _spec("24h", "Macro data"),
    SourceCategory.OPTIONS_DATA: _spec("12h", "Options data"),
    SourceCategory.YOUTUBE_MACRO: _spec("24h", "YouTube macro"),
    SourceCategory.YOUTUBE_MICRO: _spec("24h", "YouTube micro"),
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
