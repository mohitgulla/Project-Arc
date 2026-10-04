"""Source categories (D47): five equal-weight categories with a freshness window each.

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
    "SCOUT_CATEGORIES",
    "CategorySpec",
    "SourceCategory",
    "age_text",
    "earliest_ttl",
    "is_stale",
    "normalize_category",
    "parse_category",
]


class SourceCategory(enum.StrEnum):
    """D47: exactly five categories, in the fixed display order."""

    MARKET_NEWS = "market_news"
    COMPANY = "company"
    MACRO = "macro"
    OPTIONS_DATA = "options_data"
    VIDEO = "video"


CATEGORY_ORDER: tuple[SourceCategory, ...] = tuple(SourceCategory)

# Pre-D47 names, accepted for one release (logged as ``sources.category_alias``) so
# open branches and YAML keep loading.
CATEGORY_ALIASES: Mapping[str, SourceCategory] = {
    "company_news": SourceCategory.COMPANY,
    "filings": SourceCategory.COMPANY,
    "calendar": SourceCategory.COMPANY,
}

# Categories whose docs share the Scout's ``scout_doc_budget`` (raw docs). Options
# data and video reach the Director as typed context only (D45, D47).
SCOUT_CATEGORIES: frozenset[SourceCategory] = frozenset(
    {SourceCategory.MARKET_NEWS, SourceCategory.COMPANY, SourceCategory.MACRO}
)

# Typed context kinds (never raw docs) and the category they report under.
KIND_CATEGORY: Mapping[str, SourceCategory] = {
    "vol_term": SourceCategory.OPTIONS_DATA,
    "put_call": SourceCategory.OPTIONS_DATA,
    "unusual_options": SourceCategory.OPTIONS_DATA,
    "ex_dividend": SourceCategory.OPTIONS_DATA,
    "macro_calendar": SourceCategory.MACRO,
    "channel_brief": SourceCategory.VIDEO,
    # D46 Finnhub per-ticker kinds (E4.8); only present once that card lands.
    "earnings_history": SourceCategory.COMPANY,
    "insider_activity": SourceCategory.COMPANY,
    "analyst_recs": SourceCategory.COMPANY,
    "fundamentals": SourceCategory.COMPANY,
}


def parse_category(raw: Any, *, where: str = "") -> SourceCategory:
    """Strict config parse: a D47 name or a logged pre-D47 alias; anything else raises."""
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
    names = " | ".join(c.value for c in SourceCategory)
    msg = f"unknown source category {raw!r}{f' ({where})' if where else ''}; expected {names}"
    raise ValueError(msg)


def normalize_category(raw: Any) -> SourceCategory | None:
    """Lenient read of a *stored* category value (pre-D47 rows); never logs or raises."""
    text = str(raw or "").strip().lower()
    if text in SourceCategory.__members__.values():
        return SourceCategory(text)
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
    SourceCategory.COMPANY: _spec("24h", "Company"),
    SourceCategory.MACRO: _spec("24h", "Macro"),
    SourceCategory.OPTIONS_DATA: _spec("1 session", "Options data"),
    SourceCategory.VIDEO: _spec("24h", "YouTube"),
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
