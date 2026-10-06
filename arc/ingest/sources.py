"""Source registry + fair Scalp selection (E4.5 / D30, categories D47). Deterministic.

Every ingest source is a named entry built from ``config/routines.yaml``:

* each ``sources.<job>`` that writes context is one source, with ``category`` /
  ``weight`` / ``max_docs_per_run`` / ``label`` / ``max_age`` / ``age_basis`` options;
* the ``rss`` job's ``feeds`` expand to one source **per feed**. A feed is a mapping
  ``{name, url, category, weight, max_docs_per_run, label, hosts, max_age}`` (a plain
  URL string still loads, but then the job itself must declare ``category``).

Categories (D47, D49): every source belongs to one of six
:class:`~arc.context.categories.SourceCategory` values, declared in the top-level
``categories:`` block with a ``weight`` and a freshness ``max_age``. **Categories are
weighted equally** (``weight: 1`` each); a source's ``weight`` is its share *inside*
its category, so adding a feed splits its category's share instead of growing it.

Budget: ``scalp_doc_budget`` docs per Scalp run are shared by a **two-level weighted
deficit round-robin**: each pick goes first to the category furthest below its
share (``picked / category weight`` smallest; ties by name), then, inside that
category, to the source furthest below its share. Newest doc first within a source.
A source that runs out of docs (or hits ``max_docs_per_run``) stops competing, and
a category with no fresh docs left stops competing, so unused share flows to the
others. Only :data:`~arc.context.categories.SCALP_CATEGORIES` take part; options
data and the two YouTube categories reach Research as typed context (D45, D47,
D49). A YouTube channel declares its own category (``youtube_macro`` or
``youtube_micro``); channels split their category's share (:meth:`share_in_category`).

Freshness: a doc older than its source's ``max_age`` (default: its category's) is
never selected; the Scalp closes it ``skipped_stale``.

Fairness invariant (property-tested): after selection, a category that still has
unselected docs under its caps is never more than one pick behind any other
category relative to the category weights; inside a category the same holds per
source relative to the source weights.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.context.categories import (
    CATEGORY_ORDER,
    DEFAULT_CATEGORIES,
    SCALP_CATEGORIES,
    CategorySpec,
    SourceCategory,
    earliest_ttl,
    parse_category,
    parse_youtube_category,
)
from arc.context.ttl import Ttl

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from arc.routines.config import RoutinesConfig

log = structlog.get_logger()

__all__ = [
    "SCALP_EXCLUDED",
    "CategoryMix",
    "FeedSpec",
    "Selection",
    "SourceCategory",
    "SourceRegistry",
    "SourceSpec",
    "select_fair",
]

# Category when a legacy row / removed source has no registry entry (by key prefix).
DEFAULT_CATEGORY: Mapping[str, SourceCategory] = {
    "rss": SourceCategory.MARKET_NEWS,
    "edgar": SourceCategory.COMPANY_DATA,
    "earnings": SourceCategory.COMPANY_DATA,
    # A removed channel's legacy rows (never Scalp-read either way, D45); a configured
    # channel always resolves through its own ``category:`` (D49).
    "youtube": SourceCategory.YOUTUBE_MICRO,
}
UNKNOWN_CATEGORY = SourceCategory.MARKET_NEWS
# D45/D47: categories the 30-min Scalp never reads (typed context only). The registry
# still lists their sources (labels, the Tower's sources page).
SCALP_EXCLUDED: frozenset[SourceCategory] = frozenset(set(SourceCategory) - SCALP_CATEGORIES)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
STALE_GRACE = _dt.timedelta(hours=2)  # D47 context TTL: max_age + 2h (capped at the policy)

AgeBasis = Literal["published", "ingested"]
Feed = Literal["scalp", "scout"]
FEEDS: tuple[Feed, ...] = ("scalp", "scout")


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
    max_age: Ttl | None = None
    # D54: the feed's Research feed (default: the job's ``feed:``, else ``scalp``).
    feed: Feed | None = None
    # D55 (E4.11): title regexes (case-insensitive ``re.search``). An entry whose title
    # matches any ``title_exclude``, or (when ``title_include`` is set) none of
    # ``title_include``, is stored closed ``scalp_status='filtered'`` (never read).
    title_exclude: list[str] = Field(default_factory=list)
    title_include: list[str] = Field(default_factory=list)

    @field_validator("title_exclude", "title_include")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as exc:
                msg = f"invalid title filter pattern {p!r}: {exc}"
                raise ValueError(msg) from exc
        return v

    def title_filtered(self, title: str | None) -> bool:
        """D55: is an entry with *title* filtered out (stored ``filtered``, never read)?"""
        text = title or ""
        if any(re.search(p, text, flags=re.IGNORECASE) for p in self.title_exclude):
            return True
        return bool(self.title_include) and not any(
            re.search(p, text, flags=re.IGNORECASE) for p in self.title_include
        )

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is not None and not _NAME_RE.match(v):
            msg = f"feed name must be lower-case [a-z0-9_.], got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: Any) -> Any:
        return None if v is None else parse_category(v, where="feed")

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
    max_age: Ttl | None = None  # per-source override; None = the category's
    age_basis: AgeBasis = "published"  # D47: earnings rows age from the latest pull
    # D54: which Research feed the source belongs to (by refresh cadence). Only
    # ``scalp`` sources are read by the 30-min Scalp and share ``scalp_doc_budget``.
    feed: Feed = "scalp"

    @property
    def display(self) -> str:
        return self.label or self.key


# D55 (E4.11): retired RSS feed keys, kept readable for one release so stored
# ``raw_docs.source_key`` history keeps its label and category. Remove after E4.11+1.
LEGACY_SOURCES: Mapping[str, SourceSpec] = {
    "cnbc": SourceSpec(
        key="cnbc",
        job="rss",
        category=SourceCategory.MARKET_NEWS,
        label="CNBC",
        hosts=("cnbc.com",),
    ),
}
LEGACY_REPLACED_BY: Mapping[str, str] = {"cnbc": "cnbc_earnings, cnbc_business"}


def _job_category(job: str, options: Mapping[str, Any]) -> SourceCategory | None:
    raw = options.get("category")
    return parse_category(raw, where=f"sources.{job}") if raw is not None else None


def _ttl_opt(raw: Any) -> Ttl | None:
    return None if raw is None else Ttl.model_validate(raw)


def _age_basis(raw: Any) -> AgeBasis:
    if raw in (None, "published"):
        return "published"
    if raw == "ingested":
        return "ingested"
    msg = f"age_basis must be 'published' or 'ingested', got {raw!r}"
    raise ValueError(msg)


def _feed(raw: Any) -> Feed:
    """D54: a source's ``feed:`` (default ``scalp``; config load validates it vs cadence).

    D56: the pre-rename ``feed: sweep`` still reads as ``scalp`` for one release."""
    if raw is None or raw in ("scalp", "sweep"):
        return "scalp"
    if raw == "scout":
        return "scout"
    msg = f"feed must be 'scalp' or 'scout', got {raw!r}"
    raise ValueError(msg)


@dataclass(frozen=True)
class SourceRegistry:
    """All registry sources, keyed by source key (stable config order)."""

    sources: Mapping[str, SourceSpec]
    categories: Mapping[SourceCategory, CategorySpec] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORIES)
    )

    # -- construction -------------------------------------------------------

    @classmethod
    def from_routines(cls, routines: RoutinesConfig) -> SourceRegistry:
        out: dict[str, SourceSpec] = {}
        for job, spec in routines.sources.items():
            if not spec.enabled or "raw_doc_ref" not in (spec.writes or []):
                continue
            opts = spec.options
            category = _job_category(job, opts)
            job_age = _ttl_opt(opts.get("max_age"))
            basis = _age_basis(opts.get("age_basis"))
            feed_of = _feed(opts.get("feed"))
            feeds = opts.get("feeds")
            if feeds:
                for raw in feeds:
                    feed = FeedSpec.parse(raw)
                    key = feed.key
                    if key in out:
                        msg = f"duplicate source key {key!r} (feed {feed.url})"
                        raise ValueError(msg)
                    cat = feed.category or category
                    if cat is None:  # routines validation already refuses this
                        msg = f"feed {key!r}: no category (D47)"
                        raise ValueError(msg)
                    out[key] = SourceSpec(
                        key=key,
                        job=job,
                        category=cat,
                        weight=feed.weight,
                        max_docs_per_run=feed.max_docs_per_run,
                        label=feed.label or "",
                        url=feed.url,
                        hosts=feed.match_hosts,
                        max_age=feed.max_age or job_age,
                        age_basis=basis,
                        feed=feed.feed or feed_of,
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
                        # D49: the channel's own category (youtube_macro | youtube_micro)
                        category=parse_youtube_category(
                            raw.get("category"), where=f"sources.{job}.channels.{slug}"
                        ),
                        label=str(raw.get("label") or slug),
                        channel=str(raw.get("channel") or "") or None,
                        max_age=job_age,
                        feed=feed_of,
                    )
                continue
            if category is None:
                msg = f"source {job!r}: no category (D47)"
                raise ValueError(msg)
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
                max_age=job_age,
                age_basis=basis,
                feed=feed_of,
            )
        cats = {c: routines.category_spec(c) for c in SourceCategory}
        scalp = routines.personas.get("scalp")
        legacy = (scalp.options.get("category_weights") if scalp is not None else None) or {}
        if legacy:  # pre-D47 knob, aliased for one release
            log.warning("sources.category_weights_alias", superseded_by="categories.<c>.weight")
            for raw_cat, w in legacy.items():
                c = parse_category(raw_cat, where="personas.scalp.category_weights")
                if float(w) < 0:
                    msg = "category_weights must be >= 0"
                    raise ValueError(msg)
                cats[c] = cats[c].model_copy(update={"weight": float(w)})
        return cls(sources=out, categories=cats)

    # -- lookups ------------------------------------------------------------

    def feeds_for(self, job: str) -> list[SourceSpec]:
        return [s for s in self.sources.values() if s.job == job and s.url]

    def category_spec(self, category: SourceCategory) -> CategorySpec:
        return self.categories.get(category) or DEFAULT_CATEGORIES[category]

    def category_weights(
        self, present: Iterable[SourceCategory] | None = None
    ) -> dict[SourceCategory, float]:
        """D47 category share (sums to 1) over Scalp categories that *have docs*.

        *present* = categories with fresh docs this run; default: every Scalp
        category that has at least one registered source. A category absent from
        *present*, or with weight 0, gets no share (it flows to the others).
        """
        have = {
            s.category
            for s in self.sources.values()
            if s.category in SCALP_CATEGORIES and s.feed == "scalp"
        }
        cats = have if present is None else set(present) & SCALP_CATEGORIES
        raw = {c: self.category_spec(c).weight for c in cats}
        raw = {c: w for c, w in raw.items() if w > 0}
        total = sum(raw.values())
        if total <= 0:
            return {}
        return {c: raw[c] / total for c in CATEGORY_ORDER if c in raw}

    def effective_weights(
        self, present: Iterable[SourceCategory] | None = None
    ) -> dict[str, float]:
        """Category share × source share within the category (sums to 1).

        The single place category shares are computed (the Tower calls it too).
        Only Scalp-readable sources get a weight; options data and video never
        take a share of the doc budget (D45, D47).
        """
        cat_w = self.category_weights(present)
        by_cat: dict[SourceCategory, list[SourceSpec]] = {}
        for s in self.sources.values():
            if s.category in cat_w and s.feed == "scalp":  # D54: slow feed draws no budget
                by_cat.setdefault(s.category, []).append(s)
        out: dict[str, float] = {}
        for c, ss in by_cat.items():
            inner = sum(s.weight for s in ss)
            for s in ss:
                out[s.key] = cat_w[c] * (s.weight / inner)
        return out

    def share_in_category(self, key: str) -> float:
        """A source's share *inside* its category (its weight over the category total).

        D49: two ``youtube_macro`` channels get 0.5 each, three ``youtube_micro``
        channels 1/3 each, so a new channel splits its category's share. Unknown
        keys get 0.
        """
        spec = self.sources.get(key)
        if spec is None:
            return 0.0
        total = sum(s.weight for s in self.sources.values() if s.category is spec.category)
        return spec.weight / total if total > 0 else 0.0

    def max_age_for(self, key: str) -> Ttl:
        """D47 freshness window of a source: its own ``max_age`` else its category's."""
        spec = self.spec_for(key)
        return spec.max_age or self.category_spec(spec.category).max_age

    def is_stale(
        self,
        key: str,
        *,
        published: _dt.datetime | None,
        ingested: _dt.datetime | None,
        now: _dt.datetime,
    ) -> bool:
        """Is a doc of source *key* past its window at *now*? (age basis per source)."""
        spec = self.spec_for(key)
        at = ingested if spec.age_basis == "ingested" else published
        at = at or ingested or published
        if at is None:
            return False
        return self.max_age_for(key).expires_at(at) <= now

    def freshness_ttl(self, keys: Iterable[str], base: Ttl | None, at: _dt.datetime) -> Ttl | None:
        """D47 context TTL: ``min(base, shortest max_age among *keys* + 2h)``.

        Session-based windows (options data) are used as they are (no grace).
        """
        cands: list[Ttl] = [] if base is None else [base]
        windows = [self.max_age_for(k) for k in dict.fromkeys(keys)]
        if windows:
            shortest = earliest_ttl(windows, at)
            assert shortest is not None
            if shortest.duration is not None:
                cands.append(Ttl(duration=shortest.duration + STALE_GRACE))
            else:
                cands.append(shortest)
        return earliest_ttl(cands, at)

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
        """Spec for *key*; unknown keys (legacy / removed sources) get a default spec.

        D55: a retired feed key in :data:`LEGACY_SOURCES` (``cnbc``) keeps its old label
        and category for one release, so its ``raw_docs`` history still reads right.
        """
        if key in self.sources:
            return self.sources[key]
        legacy = LEGACY_SOURCES.get(key)
        if legacy is not None:
            log.debug("sources.legacy_alias", key=key, replaced_by=LEGACY_REPLACED_BY.get(key))
            return legacy
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
    category_of: Mapping[str, str] = field(default_factory=dict)
    category_weights: Mapping[str, float] = field(default_factory=dict)

    def over_budget(self) -> dict[str, int]:
        return {k: self.available[k] - self.picked.get(k, 0) for k in self.available}

    def category_picked(self) -> dict[str, int]:
        out: Counter[str] = Counter()
        for k, n in self.picked.items():
            out[self.category_of.get(k, "")] += n
        return dict(out)


def select_fair(
    docs_by_source: Mapping[str, Sequence[str]],
    weights: Mapping[str, float],
    budget: int,
    *,
    caps: Mapping[str, int | None] | None = None,
    categories: Mapping[str, str] | None = None,
    category_weights: Mapping[str, float] | None = None,
) -> Selection:
    """Two-level weighted deficit round-robin over *docs_by_source* (each newest first).

    *categories* maps a source to its category and *category_weights* gives each
    category's weight; without them every source is in one category (plain per-source
    round-robin). *weights* are the per-source weights inside a category. Sources
    missing from *weights* get the smallest known weight (never zero); categories
    missing from *category_weights* get the smallest known category weight, so a doc
    from an unregistered source is still read, just never favoured.
    """
    caps = caps or {}
    cat_of = {k: (categories or {}).get(k, "") for k in docs_by_source}
    floor = min((w for w in weights.values() if w > 0), default=1.0)
    w = {k: (weights.get(k) or floor) for k in docs_by_source}
    cw_in = category_weights or {}
    cfloor = min((v for v in cw_in.values() if v > 0), default=1.0)
    cw = {c: (cw_in.get(c) or cfloor) for c in set(cat_of.values())}
    limit = {
        k: min(len(v), caps[k] if caps.get(k) is not None else len(v))  # type: ignore[type-var]
        for k, v in docs_by_source.items()
    }
    picked: Counter[str] = Counter()
    cat_picked: Counter[str] = Counter()
    selected: list[str] = []
    active = sorted(k for k in docs_by_source if limit[k] > 0)
    while len(selected) < budget and active:
        cats = sorted({cat_of[k] for k in active})
        c = min(cats, key=lambda x: ((cat_picked[x] + 1) / cw[x], x))
        k = min((s for s in active if cat_of[s] == c), key=lambda s: ((picked[s] + 1) / w[s], s))
        selected.append(docs_by_source[k][picked[k]])
        picked[k] += 1
        cat_picked[c] += 1
        if picked[k] >= limit[k]:
            active.remove(k)
    return Selection(
        selected=selected,
        picked=dict(picked),
        available={k: len(v) for k, v in docs_by_source.items()},
        weights=w,
        category_of=cat_of,
        category_weights=cw,
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


@dataclass(frozen=True)
class CategoryMix:
    """D47 Scalp card row: one category's share and its sources' accounting.

    ``sources`` = ``[(label, picked, over_budget, stale)]`` in registry order.
    """

    category: str
    label: str
    share: float
    picked: int
    sources: list[tuple[str, int, int, int]]


def category_mix(
    selection: Selection | None,
    registry: SourceRegistry,
    *,
    stale: Mapping[str, int] | None = None,
) -> list[CategoryMix]:
    """The Scalp card's grouped source mix (fixed category order, Scalp categories only).

    A source shows up when it had fresh docs or stale ones this run; a category
    shows up when any of its sources did.
    """
    stale = stale or {}
    avail = dict(selection.available) if selection else {}
    picked = dict(selection.picked) if selection else {}
    over = selection.over_budget() if selection else {}
    shares = (
        {SourceCategory(c): v for c, v in _norm(selection.category_weights).items() if c}
        if selection
        else {}
    )
    keys = list(registry.sources)
    keys += sorted(k for k in {*avail, *stale} if k not in keys)
    out: list[CategoryMix] = []
    for cat in CATEGORY_ORDER:
        if cat not in SCALP_CATEGORIES:
            continue
        rows = [
            (
                registry.spec_for(k).display,
                picked.get(k, 0),
                over.get(k, 0),
                stale.get(k, 0),
            )
            for k in keys
            if registry.spec_for(k).category is cat and (avail.get(k) or stale.get(k))
        ]
        if not rows:
            continue
        out.append(
            CategoryMix(
                category=cat.value,
                label=registry.category_spec(cat).label,
                share=round(shares.get(cat, 0.0), 4),
                picked=sum(r[1] for r in rows),
                sources=rows,
            )
        )
    return out


def _norm(weights: Mapping[str, float]) -> dict[str, float]:
    total = sum(v for v in weights.values() if v > 0)
    return {k: (v / total if total else 0.0) for k, v in weights.items()}
