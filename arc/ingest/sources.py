"""Source registry + fair Scout selection (E4.5, D30). Deterministic, no LLM.

Every ingest source is a named entry built from ``config/routines.yaml``:

* each ``sources.<job>`` that writes ``raw_doc_ref`` is one source, with
  ``category`` / ``weight`` / ``max_docs_per_run`` / ``label`` job options;
* the ``rss`` job's ``feeds`` expand to one source **per feed**. A feed is either
  a plain URL string (PR #40 shape; name derived from the host) or a mapping
  ``{name, url, category, weight, max_docs_per_run, label, hosts}``.

Budget: ``scout_doc_budget`` docs per Scout run are shared across sources by
**weighted deficit round-robin**. Each source's effective weight is
``category_share × weight / Σ weights in its category``, where the category share
is ``category_weights[category]`` (``personas.scout.category_weights``) or, by
default, the sum of its sources' weights, so default weights are equal per
source. Picks go one doc at a time to the source furthest below its share
(``picked / weight`` smallest; ties by name), newest doc first within a source.
A source that runs out of docs (or hits ``max_docs_per_run``) simply stops
competing, so its unused share flows to the others.

Fairness invariant (property-tested): after selection, a source with unselected
docs left (and under its cap) is never more than one pick behind any other
source relative to their weights.
"""

from __future__ import annotations

import enum
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from arc.routines.config import RoutinesConfig

__all__ = [
    "FeedSpec",
    "Selection",
    "SourceCategory",
    "SourceRegistry",
    "SourceSpec",
    "select_fair",
]


class SourceCategory(enum.StrEnum):
    MARKET_NEWS = "market_news"
    COMPANY_NEWS = "company_news"
    MACRO = "macro"
    FILINGS = "filings"
    OPTIONS_DATA = "options_data"
    CALENDAR = "calendar"
    VIDEO = "video"


# Category when a source job does not declare one (by job-name prefix).
DEFAULT_CATEGORY: Mapping[str, SourceCategory] = {
    "rss": SourceCategory.MARKET_NEWS,
    "edgar": SourceCategory.FILINGS,
    "earnings": SourceCategory.CALENDAR,
    "youtube": SourceCategory.VIDEO,
}
UNKNOWN_CATEGORY = SourceCategory.MARKET_NEWS
# D45 (E4.6): categories the 30-min Scout never reads. Video reaches the trading
# loop only through the daily ``youtube.briefs`` job; the registry still lists the
# channels (labels, the Tower's sources page).
SCOUT_EXCLUDED: frozenset[SourceCategory] = frozenset({SourceCategory.VIDEO})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]*$")


def registered_domain(host: str) -> str:
    """``feeds.content.dowjones.io`` -> ``dowjones.io`` (last two labels, no ``www.``)."""
    parts = host.lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "feed"


class FeedSpec(BaseModel):
    """One RSS feed entry (``sources.rss.feeds[]``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(..., min_length=8)
    name: str | None = None
    label: str | None = None
    category: SourceCategory | None = None
    weight: float = Field(1.0, gt=0, le=100)
    max_docs_per_run: int | None = Field(None, ge=0)
    hosts: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is not None and not _NAME_RE.match(v):
            msg = f"feed name must be lower-case [a-z0-9_.], got {v!r}"
            raise ValueError(msg)
        return v

    @classmethod
    def parse(cls, raw: Any) -> FeedSpec:
        return cls(url=raw) if isinstance(raw, str) else cls.model_validate(raw)

    @property
    def key(self) -> str:
        if self.name:
            return self.name
        return _slug(registered_domain(urlsplit(self.url).netloc).split(".")[0])

    @property
    def match_hosts(self) -> tuple[str, ...]:
        """Article hosts that identify this feed's docs (legacy rows had no source key)."""
        hosts = [registered_domain(h) for h in self.hosts]
        hosts.append(registered_domain(urlsplit(self.url).netloc))
        return tuple(dict.fromkeys(hosts))


@dataclass(frozen=True)
class SourceSpec:
    """A registry source: one feed or one source job."""

    key: str
    job: str
    category: SourceCategory
    weight: float = 1.0
    max_docs_per_run: int | None = None
    label: str = ""
    url: str | None = None
    hosts: tuple[str, ...] = ()
    channel: str | None = None  # youtube channel id / url

    @property
    def display(self) -> str:
        return self.label or self.key


def _job_category(job: str, options: Mapping[str, Any]) -> SourceCategory:
    raw = options.get("category")
    if raw is not None:
        return SourceCategory(raw)
    return DEFAULT_CATEGORY.get(job.split(".", 1)[0], UNKNOWN_CATEGORY)


@dataclass(frozen=True)
class SourceRegistry:
    """All registry sources, keyed by source key (stable config order)."""

    sources: Mapping[str, SourceSpec]
    category_weights: Mapping[SourceCategory, float] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_routines(cls, routines: RoutinesConfig) -> SourceRegistry:
        out: dict[str, SourceSpec] = {}
        for job, spec in routines.sources.items():
            if not spec.enabled or "raw_doc_ref" not in (spec.writes or []):
                continue
            opts = spec.options
            category = _job_category(job, opts)
            feeds = opts.get("feeds")
            if feeds:
                for raw in feeds:
                    feed = FeedSpec.parse(raw)
                    key = feed.key
                    if key in out:
                        msg = f"duplicate source key {key!r} (feed {feed.url})"
                        raise ValueError(msg)
                    out[key] = SourceSpec(
                        key=key,
                        job=job,
                        category=feed.category or category,
                        weight=feed.weight,
                        max_docs_per_run=feed.max_docs_per_run,
                        label=feed.label or "",
                        url=feed.url,
                        hosts=feed.match_hosts,
                    )
                continue
            channels = opts.get("channels")
            if channels:  # E4.6: one registry source per YouTube channel (youtube.<slug>)
                for raw in channels:
                    slug = str(raw.get("slug") or "")
                    key = f"youtube.{slug}"
                    if not slug or key in out:
                        msg = f"duplicate or empty channel source key {key!r}"
                        raise ValueError(msg)
                    out[key] = SourceSpec(
                        key=key,
                        job=job,
                        category=category,
                        label=str(raw.get("label") or slug),
                        channel=str(raw.get("channel") or "") or None,
                    )
                continue
            if job in out:
                msg = f"duplicate source key {job!r}"
                raise ValueError(msg)
            weight = float(opts.get("weight", 1.0))
            if weight <= 0:
                msg = f"source {job!r}: weight must be > 0"
                raise ValueError(msg)
            cap = opts.get("max_docs_per_run")
            out[job] = SourceSpec(
                key=job,
                job=job,
                category=category,
                weight=weight,
                max_docs_per_run=int(cap) if cap is not None else None,
                label=str(opts.get("label") or ""),
                channel=opts.get("channel"),
            )
        scout = routines.personas.get("scout")
        raw_cw = (scout.options.get("category_weights") if scout is not None else None) or {}
        cw = {SourceCategory(k): float(v) for k, v in raw_cw.items()}
        if any(v <= 0 for v in cw.values()):
            msg = "category_weights must be > 0"
            raise ValueError(msg)
        return cls(sources=out, category_weights=cw)

    # -- lookups ------------------------------------------------------------

    def feeds_for(self, job: str) -> list[SourceSpec]:
        return [s for s in self.sources.values() if s.job == job and s.url]

    def effective_weights(self) -> dict[str, float]:
        """Category share × source share within the category (sums to 1).

        Only Scout-readable sources get a weight: :data:`SCOUT_EXCLUDED` categories
        (video, D45) are left out, so they never take a share of the doc budget.
        """
        by_cat: dict[SourceCategory, list[SourceSpec]] = {}
        for s in self.sources.values():
            if s.category in SCOUT_EXCLUDED:
                continue
            by_cat.setdefault(s.category, []).append(s)
        if not by_cat:
            return {}
        cat_w = {
            c: self.category_weights.get(c, sum(s.weight for s in ss)) for c, ss in by_cat.items()
        }
        total = sum(cat_w.values())
        out: dict[str, float] = {}
        for c, ss in by_cat.items():
            inner = sum(s.weight for s in ss)
            for s in ss:
                out[s.key] = (cat_w[c] / total) * (s.weight / inner)
        return out

    def key_for(self, row: Mapping[str, Any]) -> str:
        """Registry key of a ``raw_docs`` row (``source_key`` column, else derived)."""
        key = row.get("source_key")
        if key:
            return str(key)
        source = str(row.get("source") or "unknown")
        if source == "rss":
            domain = registered_domain(urlsplit(str(row.get("url") or "")).netloc)
            for s in self.sources.values():
                if s.url and domain in s.hosts:
                    return s.key
            return "rss"
        if source == "youtube" and row.get("channel_id"):
            ch = str(row["channel_id"])
            for s in self.sources.values():
                if s.channel and ch in s.channel:
                    return s.key
        if source in self.sources:
            return source
        for s in self.sources.values():
            if s.job.split(".", 1)[0] == source:
                return s.key
        return source

    def spec_for(self, key: str) -> SourceSpec:
        """Spec for *key*; unknown keys (legacy / removed sources) get a default spec."""
        if key in self.sources:
            return self.sources[key]
        prefix = key.split(".", 1)[0]
        return SourceSpec(key=key, job=key, category=DEFAULT_CATEGORY.get(prefix, UNKNOWN_CATEGORY))


# ---------------------------------------------------------------------------
# Fair selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """Result of :func:`select_fair`: chosen doc ids plus per-source accounting."""

    selected: list[str]
    picked: Mapping[str, int]
    available: Mapping[str, int]
    weights: Mapping[str, float]

    def over_budget(self) -> dict[str, int]:
        return {k: self.available[k] - self.picked.get(k, 0) for k in self.available}


def select_fair(
    docs_by_source: Mapping[str, Sequence[str]],
    weights: Mapping[str, float],
    budget: int,
    *,
    caps: Mapping[str, int | None] | None = None,
) -> Selection:
    """Weighted deficit round-robin over *docs_by_source* (each list newest first).

    Sources missing from *weights* get the smallest known weight (never zero), so a
    doc from an unregistered source is still read, just never favoured.
    """
    caps = caps or {}
    floor = min((w for w in weights.values() if w > 0), default=1.0)
    w = {k: (weights.get(k) or floor) for k in docs_by_source}
    limit = {
        k: min(len(v), caps[k] if caps.get(k) is not None else len(v))  # type: ignore[type-var]
        for k, v in docs_by_source.items()
    }
    picked: Counter[str] = Counter()
    selected: list[str] = []
    active = sorted(k for k in docs_by_source if limit[k] > 0)
    while len(selected) < budget and active:
        k = min(active, key=lambda s: ((picked[s] + 1) / w[s], s))
        selected.append(docs_by_source[k][picked[k]])
        picked[k] += 1
        if picked[k] >= limit[k]:
            active.remove(k)
    return Selection(
        selected=selected,
        picked=dict(picked),
        available={k: len(v) for k, v in docs_by_source.items()},
        weights=w,
    )


def format_source_mix(
    selection: Selection, registry: SourceRegistry, *, order: Iterable[str] | None = None
) -> list[tuple[str, int, int]]:
    """``[(label, picked, over_budget)]`` in registry order, then any unknown sources."""
    keys = list(order or registry.sources)
    keys += sorted(k for k in selection.available if k not in keys)
    over = selection.over_budget()
    return [
        (registry.spec_for(k).display, selection.picked.get(k, 0), over.get(k, 0))
        for k in keys
        if selection.available.get(k)
    ]
