"""Research's idea pool (E13.8, D56/D53): Scalp + Scout candidates merged by ticker.

Pure and deterministic (no LLM, no network, no wall clock). The Research step builds
the pool from its context snapshot and records it in the prompt inputs, so ``arc
journal replay`` renders the identical prompt from the recorded pool.

Where the feeds come from. Both personas write ``candidate`` context entries
(subject = ticker, supersede latest) from the same per-day ``candidates`` row, which
:func:`arc.ingest.scalp.store_candidate` merges across feeds. A Scout source is a
validated ``youtube:<slug>`` origin and a Scalp source is a document URL, so an
entry's feeds are read from its sources (plus ``feed == "scout"``), never from the
``feed`` field alone: a Scalp run re-writes every candidate of the day, Scout rows
included, as ``feed=scalp``.

Rules (D53, card E13.8):

- ``feeds`` from the sources; ``origins`` = distinct Scalp registry sources
  (``corroboration``, else distinct URLs) + distinct Scout channel origins.
- ``stance`` = the stored (merged) stance: the higher-confidence feed's, a tie
  collapsing to neutral (``merge_candidates``). ``confidence`` = the max over feeds:
  the stored value, or the Scout call's when the Scout's stance won.
- ``agreement``: one feed -> ``single``; the Scout call (latest ``scout_read``) has
  the stored stance and no lower confidence -> ``agree``; else ``disagree``. No Scout
  call on record -> ``agree`` (only the merged view is known).
- Scout-only tickers are ordered by confidence, then origins, and cut at
  ``funnel.research.max_scout_only_ideas`` (journaled ``over_scout_only_cap``).
- ``scalp`` mode (the control): the pool is the Scalp-sourced entries only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arc.personas.schemas import PoolItem

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from arc.context.store import ContextEntry, ContextSnapshot

__all__ = [
    "EXIT_BLOCK_RESERVE_CHARS",
    "POOL_BUDGET_CUT",
    "POOL_MAX_LINES",
    "SCOUT_ORIGIN_PREFIX",
    "IdeaPool",
    "build_idea_pool",
    "cut_pool",
    "entry_feeds",
    "pool_order",
    "scalp_entries",
]

#: E13.17's exit block (<= 600 chars x 12 positions) is reserved inside
#: ``research_prompt_max_chars``: the compact prompt without it must fit the rest.
EXIT_BLOCK_RESERVE_CHARS = 7_200
#: The compact prompt lists at most this many pool lines (one per ticker).
POOL_MAX_LINES = 60
#: Over the prompt budget (after the headlines are trimmed) the pool is cut to this.
POOL_BUDGET_CUT = 40
SCOUT_ORIGIN_PREFIX = "youtube:"


def _sources(payload: Mapping[str, Any]) -> list[str]:
    return [str(s) for s in payload.get("sources") or []]


def entry_feeds(payload: Mapping[str, Any]) -> set[str]:
    """``{"scalp", "scout"}`` subset a stored candidate payload came from."""
    srcs = _sources(payload)
    feeds: set[str] = set()
    if payload.get("feed") == "scout" or payload.get("origins"):
        feeds.add("scout")
    if any(s.startswith(SCOUT_ORIGIN_PREFIX) for s in srcs):
        feeds.add("scout")
    if any(not s.startswith(SCOUT_ORIGIN_PREFIX) for s in srcs):
        feeds.add("scalp")
    if not feeds:  # no sources at all: a pre-E13.7 Scalp row
        feeds.add("scalp")
    return feeds


def scalp_entries(entries: Iterable[ContextEntry]) -> list[ContextEntry]:
    """The candidate entries the Scalp raised (``research_idea_pool: scalp``).

    Before the Scout exists (every entry pre-E13.7) this is every entry, so the
    control's prompt is unchanged.
    """
    return [e for e in entries if "scalp" in entry_feeds(e.payload)]


def _origins(payload: Mapping[str, Any], feeds: set[str]) -> int:
    srcs = _sources(payload)
    scout = {s for s in srcs if s.startswith(SCOUT_ORIGIN_PREFIX)}
    scout |= {str(o) for o in payload.get("origins") or []}
    n = len(scout)
    if "scalp" in feeds:
        urls = {s for s in srcs if not s.startswith(SCOUT_ORIGIN_PREFIX)}
        corr = payload.get("corroboration")
        n += int(corr) if isinstance(corr, int) and corr > 0 else len(urls)
    return max(1, n)


def _scout_calls(snapshot: ContextSnapshot) -> dict[str, tuple[str, float]]:
    """ticker -> (stance, confidence) of the latest Scout read's calls."""
    read = snapshot.latest("scout_read")
    if read is None:
        return {}
    out: dict[str, tuple[str, float]] = {}
    for c in read.payload.get("ticker_calls") or []:
        t = str(c.get("ticker", "")).upper()
        if t and t not in out:
            out[t] = (str(c.get("stance", "")), float(c.get("confidence") or 0.0))
    return out


def _catalyst_date(raw: Any) -> str | None:
    text = str(raw or "").strip()
    return text[:10] if text else None


def _item(
    e: ContextEntry, calls: Mapping[str, tuple[str, float]], tiers: Mapping[str, str]
) -> PoolItem:
    p = e.payload
    feeds = entry_feeds(p)
    stance = str(p["stance"])
    conf = float(p.get("confidence") or 0.0)
    agreement = "single"
    if feeds == {"scalp", "scout"}:
        call = calls.get(e.subject)
        if call is None:
            agreement = "agree"
        else:
            c_stance, c_conf = call
            agreement = "agree" if c_stance == stance and conf >= c_conf else "disagree"
            if c_stance == stance:
                conf = max(conf, c_conf)  # the Scout's call won: its confidence
    tier = tiers.get(e.subject, "none")
    return PoolItem(
        ticker=e.subject,
        stance=stance,  # type: ignore[arg-type]
        confidence=min(1.0, conf),
        feeds=[f for f in ("scalp", "scout") if f in feeds],  # type: ignore[misc]
        origins=_origins(p, feeds),
        agreement=agreement,  # type: ignore[arg-type]
        tier=tier if tier in ("core", "momentum", "discovery") else "none",  # type: ignore[arg-type]
        candidate_ids=[str(p.get("id") or e.id)],
        catalyst_type=p.get("catalyst_type"),
        catalyst_date=_catalyst_date(p.get("catalyst_date")),
    )


def pool_order(item: PoolItem) -> tuple[float, int, str]:
    """Sort key: confidence, then origins (both descending), then ticker."""
    return (-item.confidence, -item.origins, item.ticker)


def cut_pool(items: Sequence[PoolItem], limit: int) -> tuple[list[PoolItem], list[PoolItem]]:
    """``(kept, cut)``: the top *limit* items by :func:`pool_order`, the rest."""
    ordered = sorted(items, key=pool_order)
    return ordered[: max(0, limit)], ordered[max(0, limit) :]


@dataclass(frozen=True)
class IdeaPool:
    """The pool Research ranks, plus the Scout-only ideas cut by the cap."""

    items: list[PoolItem]
    capped: list[PoolItem] = field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        return [i.ticker for i in self.items]

    def counts(self) -> dict[str, int]:
        """``pool_counts`` for the shortlist payload and the card."""
        both = sum(1 for i in self.items if len(i.feeds) == 2)
        scout = sum(1 for i in self.items if i.feeds == ["scout"])
        return {
            "scalp": len(self.items) - both - scout,
            "scout": scout,
            "both": both,
            "scout_only_capped": len(self.capped),
        }


def build_idea_pool(
    snapshot: ContextSnapshot,
    *,
    merged: bool,
    max_scout_only: int,
    tiers: Mapping[str, str] | None = None,
) -> IdeaPool:
    """The idea pool from the snapshot's active ``candidate`` entries (pure).

    *merged* = ``research_idea_pool: all``; otherwise Scalp-sourced entries only.
    *tiers* = ``ticker -> tier name`` (display only). Items are in :func:`pool_order`.
    """
    entries = snapshot.of_kind("candidate")
    if not merged:
        entries = scalp_entries(entries)
    calls = _scout_calls(snapshot)
    items = [_item(e, calls, tiers or {}) for e in entries]
    scout_only = [i for i in items if i.feeds == ["scout"]]
    kept_scout, capped = cut_pool(scout_only, max_scout_only)
    keep = {i.ticker for i in kept_scout}
    items = [i for i in items if i.feeds != ["scout"] or i.ticker in keep]
    return IdeaPool(items=sorted(items, key=pool_order), capped=capped)
