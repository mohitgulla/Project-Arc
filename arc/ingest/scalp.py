"""Scalp candidate pipeline (E4.2): RawDoc batches → validated ``Candidate`` rows.

Flow per run::

    raw_docs (unscalped) ──batch──▶ Scalp prompt ──Hermes (cheap tier)──▶ raw JSON
        ──▶ ScalpOutput schema ──▶ per-candidate filters ──▶ merge per ticker/day
        ──▶ candidates table                       (+ scalp_batches audit row)

Filters (deterministic, applied after the LLM):

* **schema** — each candidate must validate against ``ScalpCandidateOut``.
* **universe** (D28/D51, :class:`~arc.universe.guard.UniverseGuard`) — ``strict``:
  ticker must be in the active list. ``seed`` (default): core and momentum
  tickers always pass; any other ticker must be in the symbol master
  (``unknown_symbol``), optionable, under the per-run new-ticker cap
  (``over_new_ticker_cap``) and pass the liquidity screen (``illiquid``).
* **threshold** — confidence must be ``>= settings.scalp_min_confidence``. E12.4
  (D51): core and momentum tickers skip it (kept, and journaled by the scalp job
  as ``scalp_candidate`` with ``confidence_floor_skipped: tier=<tier>``).
  D56 (``tiers.model: d56``, E13.4): each tier has its own floor
  (``universe_floor_<tier>``: core 0.4, momentum 0.5, discovery 0.6); a candidate
  below it is rejected ``below_threshold`` and journaled ``confidence_floor_skipped:
  tier=<t> floor=<f>``. A name in no tier is ``not_in_tier``: never a candidate, only
  listed in :attr:`ScalpRunResult.mentions`.
* **sources** — only URLs of documents actually in the batch survive; a
  candidate with no grounded source is dropped (no hallucinated citations).

Options tape (E13.10, ``personas.scalp_options_tape: on``): stage 2 also reads the
code-built Cboe tape (:func:`arc.ingest.cboe_fast.scalp_tape`), outside the doc budget.
It never creates or removes a candidate: an accepted candidate whose stance matches
its ticker's P/C direction gets the :data:`~arc.ingest.cboe_fast.TAPE_SOURCE` token,
which counts as one more distinct source in ``corroboration``.

The liquidity screen runs last (after threshold and sources), so market data is
only fetched for candidates that would otherwise be accepted.

Funnel discipline: the only thing downstream code (scanner, Research) may
read is :func:`candidates_for_scanner`, which returns ``Candidate`` models
— enums, symbols, numbers, dates and source URLs. Persona free text
(rationale, scan summary, the verbatim response) is stored in
``scalp_batches`` for audit and never leaves it.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arc.context.categories import LEGACY_VIDEO, REFERENCE, SCALP_CATEGORIES
from arc.context.kinds import StoryEvidence, StoryPayload
from arc.ingest.cboe_fast import TAPE_SOURCE, ScalpTape, tape_corroborates
from arc.ingest.llm import FixtureScalpLLM, HermesScalpLLM, LLMResult, ScalpLLMError
from arc.ingest.sources import (
    SCALP_EXCLUDED,
    CategoryMix,
    Selection,
    SourceRegistry,
    category_mix,
    format_source_mix,
    select_fair,
)
from arc.ingest.store import RawDocRepo, ScalpBatchRepo
from arc.ingest.stories import ClusterDoc, Story, cluster_stories, form_type_of, headline_of
from arc.models import Candidate, CatalystType, Stance
from arc.personas.builders import (
    ScalpInput,
    StoryDigestInput,
    build_scalp_prompt,
    build_story_digest_prompt,
    ticker_facts_block,
)
from arc.personas.schemas import ScalpCandidateOut, ScalpOutput, StoryDigestOutput
from arc.store.repos import CandidateRepo
from arc.universe.guard import (
    REJECT_ILLIQUID,
    REJECT_NEW_TICKER_CAP,
    REJECT_NOT_IN_TIER,
    REJECT_NOT_IN_UNIVERSE,
    REJECT_UNKNOWN_SYMBOL,
    UniverseGuard,
)
from arc.universe.tiers import watch_tickers
from arc.utils.calendar import ET, now_et

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Collection, Mapping

    from arc.config import ArcSettings
    from arc.context.store import ContextSnapshot
    from arc.context.ttl import Ttl
    from arc.ingest.llm import PersonaLLM
    from arc.routines.config import RoutinesConfig

log = structlog.get_logger()

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "scalp"

# Rejection reasons (stable keys; stored in scalp_batches.rejected).
REJECT_SCHEMA = "schema"
REJECT_UNIVERSE = REJECT_NOT_IN_UNIVERSE  # strict mode (kept name for callers)
REJECT_THRESHOLD = "below_threshold"
REJECT_SOURCE = "no_grounded_source"
# Re-exported for callers/tests (the universe keys live in arc.universe.guard).
_UNIVERSE_REJECTS = (
    REJECT_ILLIQUID,
    REJECT_NEW_TICKER_CAP,
    REJECT_UNKNOWN_SYMBOL,
    REJECT_NOT_IN_TIER,
)

_FEED_DELIMITER = "FEEDS>>>"
_MAX_UNSCALPED_PER_RUN = 200
MAX_MENTIONS = 10  # D56: out-of-tier ideas listed per run (deduped by ticker)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class ScalpMention(BaseModel):
    """An out-of-tier idea kept as text only (D56 owner decision 2): never a candidate,
    never read by Research's pool, never a Scout input. Note and card only."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    stance: Stance
    catalyst_type: CatalystType | None = None
    headline: str = Field(default="", max_length=120)  # the story title that raised it


@dataclass
class ScalpRunResult:
    """Summary of a Scalp run. ``candidates`` is the post-merge state for the day."""

    run_id: str
    day: str
    dry_run: bool
    batches: int = 0
    failed_batches: int = 0
    docs_scalped: int = 0
    accepted: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    rejected_items: dict[str, list[str]] = field(default_factory=dict)  # reason -> tickers
    candidates: list[Candidate] = field(default_factory=list)
    # ticker -> one-line Scalp rationale (highest-confidence accepted item this run).
    # Display only (Slack digest); never copied onto ``Candidate`` (funnel discipline).
    rationales: dict[str, str] = field(default_factory=dict)
    # D27: each ok batch's ``scan_summary`` (+ the doc URLs it covered), written by the
    # scalp job as one ``note`` (topic=observation). Never copied onto ``Candidate``.
    summaries: list[str] = field(default_factory=list)
    summary_sources: list[str] = field(default_factory=list)
    # D28: ticker -> why the universe guard rejected it (screen failures etc.), and the
    # non-seed tickers admitted this run. Display + journal only.
    reject_details: dict[str, str] = field(default_factory=dict)
    new_tickers: list[str] = field(default_factory=list)
    # E12.4: ticker -> (tier, confidence) of candidates accepted this run below
    # scalp_min_confidence because their tier (core / momentum) skips the floor.
    floor_skipped: dict[str, tuple[str, float]] = field(default_factory=dict)
    # D56 (E13.4): ticker -> (tier, best confidence, floor) of ideas rejected below their
    # tier's floor this run, and the names in no tier (mentions, never candidates).
    floor_rejected: dict[str, tuple[str, float, float]] = field(default_factory=dict)
    mentions: list[ScalpMention] = field(default_factory=list)  # <= MAX_MENTIONS
    # E13.10 (personas.scalp_options_tape on): the tape stage 2 read (None = flag off)
    # and ticker -> P/C volume of candidates it corroborated this run.
    tape: ScalpTape | None = None
    tape_corroborated: dict[str, float | None] = field(default_factory=dict)
    # E4.5 (D30): fair selection + story synthesis accounting (display + manifest).
    source_mix: list[tuple[str, int, int]] = field(default_factory=list)  # label, read, over
    over_budget: int = 0
    skipped_budget: int = 0
    # D47 (E4.7): per-category freshness + grouped mix for the Scalp card.
    skipped_stale: int = 0
    # D54: docs of `feed: scout` sources (earnings calendar) closed out of the Scalp queue.
    slow_feed: int = 0
    # D55 (E4.11): docs stored `filtered` by a feed's title filter since the last Scalp
    # (claimed by this run); never read, counted here for the card and metrics.
    filtered: int = 0
    filtered_by_source: dict[str, int] = field(default_factory=dict)
    stale_by_source: dict[str, int] = field(default_factory=dict)
    category_mix: list[CategoryMix] = field(default_factory=list)
    # D47: context TTL per story id / candidate ticker (min(max_age of its sources) + 2h);
    # the job writes each entry with min(policy TTL, this).
    story_ttls: dict[str, Ttl] = field(default_factory=dict)
    candidate_ttls: dict[str, Ttl] = field(default_factory=dict)
    stories: list[StoryPayload] = field(default_factory=list)
    digest_batches: int = 0
    failed_digest_batches: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    _rationale_conf: dict[str, float] = field(default_factory=dict, repr=False)

    @property
    def tape_present(self) -> bool:
        """A fresh tape was in the prompt (flag on and index vols within max_age)."""
        return self.tape is not None and self.tape.present

    @property
    def tape_tickers(self) -> int:
        return len(self.tape.tickers) if self.tape is not None else 0

    def add_usage(self, reply: LLMResult) -> None:
        for name in ("input_tokens", "output_tokens", "cost_usd"):
            v = getattr(reply, name)
            if v is not None:
                setattr(self, name, (getattr(self, name) or 0) + v)


@dataclass(frozen=True)
class _Doc:
    id: str
    source: str
    url: str
    published_at: str
    text: str
    tickers_hint: list[str]
    title: str | None = None
    source_key: str = ""
    ingested_at: str = ""
    category: str = ""
    feed: str = "scalp"  # D54: the source's declared feed


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_ticker(raw: str) -> str:
    """Upper-case, strip whitespace and a leading ``$`` cashtag."""
    return raw.strip().lstrip("$").strip().upper()


def extract_json_object(text: str) -> Any:
    """Parse the JSON object in an LLM reply, tolerating code fences / chatter.

    Raises ``ValueError`` if no JSON object can be decoded.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


def parse_catalyst_date(raw: str | None) -> _dt.datetime | None:
    """ISO date/datetime → midnight ET on that date; ``None`` if absent or unparsable."""
    if not raw:
        return None
    try:
        d = _dt.date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None
    return _dt.datetime(d.year, d.month, d.day, tzinfo=ET)


def _merge_key(c: Candidate) -> tuple[float, str, str]:
    """Deterministic ordering used to pick a winner between two candidates."""
    date = c.catalyst_date.date().isoformat() if c.catalyst_date else ""
    return (c.confidence, c.catalyst_type.value, date)


def merge_candidates(a: Candidate, b: Candidate) -> Candidate:
    """Merge two candidates for the same ticker/day into one.

    * Sources are unioned and sorted.
    * Same stance → confidence is the max; catalyst fields come from the
      higher-ranked candidate (falling back to the other's catalyst_date).
    * Opposing stances → the higher-confidence stance wins with confidence
      reduced by the loser's (disagreement penalty); an exact tie collapses
      to ``neutral`` with confidence 0.
    * ``created_at`` is the earliest; ``id`` is kept from whichever has one.

    The merge is commutative (``id`` aside: the first non-null one is kept)
    and idempotent.
    """
    if a.ticker != b.ticker:
        msg = f"cannot merge candidates for different tickers: {a.ticker} vs {b.ticker}"
        raise ValueError(msg)

    hi, lo = (a, b) if _merge_key(a) >= _merge_key(b) else (b, a)
    if _merge_key(a) == _merge_key(b):
        # Full tie on the key: order by stance value to keep commutativity.
        hi, lo = (a, b) if a.stance.value <= b.stance.value else (b, a)

    sources = sorted(set(hi.sources) | set(lo.sources))

    if hi.stance == lo.stance:
        stance = hi.stance
        confidence = hi.confidence
    elif hi.confidence == lo.confidence:
        stance = Stance.NEUTRAL
        confidence = 0.0
    else:
        stance = hi.stance
        confidence = max(0.0, hi.confidence - lo.confidence)

    return Candidate(
        id=a.id or b.id,
        ticker=hi.ticker,
        stance=stance,
        catalyst_type=hi.catalyst_type,
        catalyst_date=hi.catalyst_date or lo.catalyst_date,
        confidence=confidence,
        sources=sources,
        created_at=min(a.created_at, b.created_at),
    )


def validate_scalp_candidate(
    item: Any,
    *,
    universe: Collection[str] | UniverseGuard,
    min_confidence: float,
    allowed_sources: frozenset[str],
    created_at: _dt.datetime,
) -> Candidate | str:
    """Turn one raw LLM candidate into a ``Candidate`` or a rejection reason.

    *universe* is either a plain allow-list (strict behaviour) or a
    :class:`UniverseGuard` (D28): the symbol check runs first, the new-ticker cap
    and liquidity screen run last, only for otherwise-valid candidates. With a
    guard, core and momentum tickers skip *min_confidence* (E12.4). Under a D56 guard
    *min_confidence* is unused: the ticker's tier floor applies (E13.4).
    """
    try:
        out = ScalpCandidateOut.model_validate(item)
    except ValidationError:
        return REJECT_SCHEMA

    ticker = normalize_ticker(out.ticker)
    guard = universe if isinstance(universe, UniverseGuard) else None
    if guard is not None:
        if (why := guard.known(ticker)) is not None:
            return why
    elif ticker not in cast("Collection[str]", universe):
        return REJECT_UNIVERSE
    if guard is not None and guard.model == "d56":
        floor = guard.floor_for(ticker)
        if floor is not None and out.confidence < floor:
            return REJECT_THRESHOLD
    elif out.confidence < min_confidence and (
        guard is None or guard.skips_confidence_floor(ticker) is None
    ):
        return REJECT_THRESHOLD

    sources = [s.strip() for s in out.sources if s.strip() in allowed_sources]
    sources = list(dict.fromkeys(sources))
    if not sources:
        return REJECT_SOURCE

    try:
        cand = Candidate(
            ticker=ticker,
            stance=out.stance,
            catalyst_type=out.catalyst_type,
            catalyst_date=parse_catalyst_date(out.catalyst_date),
            confidence=out.confidence,
            sources=sources,
            created_at=created_at,
        )
    except ValidationError:
        return REJECT_SCHEMA
    if guard is not None and (why := guard.admit(ticker)) is not None:
        return why
    return cand


def _note_floor_reject(
    result: ScalpRunResult, guard: UniverseGuard, ticker: str, item: Any
) -> None:
    """D56: remember the best confidence of an idea rejected below its tier's floor."""
    tier = guard.membership(ticker)
    floor = guard.floor_for(ticker)
    conf = item.get("confidence") if isinstance(item, dict) else None
    if tier is None or floor is None or not isinstance(conf, int | float):
        return
    prev = result.floor_rejected.get(ticker)
    if prev is None or float(conf) > prev[1]:
        result.floor_rejected[ticker] = (tier.value, float(conf), floor)


def render_doc(doc: _Doc, *, max_chars: int) -> str:
    """Render one RawDoc for the Scalp prompt (truncated, delimiter-safe)."""
    text = doc.text.replace(_FEED_DELIMITER, "FEEDS>")
    if len(text) > max_chars:
        text = text[:max_chars] + " …[truncated]"
    hints = ",".join(doc.tickers_hint) or "-"
    return (
        f"[doc {doc.id}] source={doc.source} url={doc.url} "
        f"published={doc.published_at} tickers_hint={hints}\n{text}"
    )


def build_prompt(
    docs: list[_Doc],
    settings: ArcSettings,
    day: str,
    *,
    open_universe: bool | None = None,
    universe: list[str] | None = None,
) -> str:
    """*universe* = the watch list (D51: core + momentum + trending); default the core."""
    if open_universe is None:
        open_universe = settings.universe_mode == "seed"
    if universe is None:
        from arc.universe.tiers import core_tickers

        universe = core_tickers(settings)
    return build_scalp_prompt(
        ScalpInput(
            universe=list(universe),
            raw_feeds=[render_doc(d, max_chars=settings.scalp_max_doc_chars) for d in docs],
            scan_date=day,
            min_confidence=settings.scalp_min_confidence,
            output_schema_json=json.dumps(ScalpOutput.model_json_schema(), sort_keys=True),
            open_universe=open_universe,
        )
    )


# -- E4.5 (D30) two-stage synthesis: renderers --------------------------------

_DOCS_PER_STORY = 3  # stage 1 reads at most this many docs of one story


def _safe(text: str) -> str:
    return text.replace(_FEED_DELIMITER, "FEEDS>")


def render_story(story: Story, docs: dict[str, _Doc], *, max_chars: int) -> str:
    """Stage-1 rendering: story header + up to 3 docs (one per source first)."""
    by_source: dict[str, _Doc] = {}
    for cd in story.docs:
        by_source.setdefault(cd.source_key, docs[cd.id])
    picked = list(by_source.values())[:_DOCS_PER_STORY]
    lines = [
        f"[story {story.id}] category={story.category} "
        f"distinct_sources={story.distinct_sources} docs={len(story.docs)} "
        f"headline={_safe(story.headline)[:200]}"
    ]
    for d in picked:
        text = _safe(d.text)
        if len(text) > max_chars:
            text = text[:max_chars] + " …[truncated]"
        lines.append(
            f"  (doc) source={d.source_key} url={d.url} published={d.published_at}\n{text}"
        )
    return "\n".join(lines)


def render_digest(p: StoryPayload) -> str:
    """Stage-2 rendering of one story digest (what the Scalp reads instead of docs)."""
    ev = "".join(f'\n  evidence: "{_safe(e.quote)}" ({e.url})' for e in p.evidence)
    cat = p.catalyst_type.value if p.catalyst_type else "-"
    return (
        f"[story {p.story_id}] category={p.category} distinct_sources={p.distinct_sources} "
        f"sources={','.join(p.source_keys)} tickers={','.join(p.tickers) or '-'} "
        f"catalyst={cat} catalyst_date={p.catalyst_date or '-'} "
        f"published={p.last_published}\n  urls={' '.join(p.urls)}\n  {_safe(p.summary)}{ev}"
    )


def build_digest_prompt(stories: list[str], day: str) -> str:
    return build_story_digest_prompt(
        StoryDigestInput(
            stories=stories,
            scan_date=day,
            output_schema_json=json.dumps(StoryDigestOutput.model_json_schema(), sort_keys=True),
        )
    )


def build_stage2_prompt(
    digests: list[StoryPayload],
    settings: ArcSettings,
    day: str,
    *,
    open_universe: bool,
    ticker_facts: str = "",
    universe: list[str] | None = None,
    tape: str | None = None,
) -> str:
    """*universe* = the watch list (D51: core + momentum + trending); default the core.

    *tape* (E13.10) is the options tape block; ``None`` (flag off) leaves the prompt
    byte-identical to the pre-E13.10 one.
    """
    if universe is None:
        from arc.universe.tiers import core_tickers

        universe = core_tickers(settings)
    return build_scalp_prompt(
        ScalpInput(
            universe=list(universe),
            raw_feeds=[render_digest(p) for p in digests],
            scan_date=day,
            min_confidence=settings.scalp_min_confidence,
            output_schema_json=json.dumps(ScalpOutput.model_json_schema(), sort_keys=True),
            open_universe=open_universe,
            digests=True,
            ticker_facts=ticker_facts,
            options_tape=tape or "",
        )
    )


def scalp_facts_tickers(digests: list[StoryPayload], max_tickers: int) -> list[str]:
    """E4.8a: tickers the batch's story digests name, in story order, first *max_tickers*."""
    seen: dict[str, None] = {}
    for p in digests:
        for t in p.tickers:
            t = normalize_ticker(t)
            if t:
                seen.setdefault(t, None)
    return list(seen)[: max(0, max_tickers)]


def _facts_snapshot(
    conn: sqlite3.Connection, routines: RoutinesConfig | None, now: _dt.datetime, run_id: str
) -> ContextSnapshot | None:
    """E4.8a: a recorded snapshot of the Finnhub kinds (+ regime), or None with the flag off."""
    if routines is None or not routines.finnhub_context.enabled:
        return None
    from arc.context.store import ContextStore
    from arc.routines.config import FINNHUB_FACT_KINDS

    return ContextStore(conn).snapshot(now, kinds=[*FINNHUB_FACT_KINDS, "regime"], run_id=run_id)


def _options_tape(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    routines: RoutinesConfig | None,
    now: _dt.datetime,
    run_id: str,
) -> ScalpTape | None:
    """E13.10: the options tape at the tick's *now*, or None with the flag off.

    The read is recorded as a context snapshot (audit). *now* is the real tick time,
    never a stored ``fetched_at`` (that one is truncated to the second).
    """
    if routines is None or not routines.scalp_options_tape.enabled:
        return None
    from arc.context.categories import SourceCategory
    from arc.context.store import ContextStore
    from arc.ingest.cboe_fast import scalp_tape_from_store

    kinds = ["index_vols", "chain_snapshot", "exchange_volume"]
    ContextStore(conn).snapshot(now, kinds=kinds, run_id=run_id)
    max_age = routines.category_spec(SourceCategory.OPTIONS_FAST).max_age.duration
    return scalp_tape_from_store(
        conn,
        now,
        max_age or _dt.timedelta(minutes=30),
        max_chars=settings.scalp_tape_max_chars,
        pc_bull=settings.scalp_tape_pc_bull,
        pc_bear=settings.scalp_tape_pc_bear,
    )


def with_tape_source(c: Candidate, tape: ScalpTape | None) -> Candidate:
    """E13.10: add :data:`TAPE_SOURCE` when the ticker's tape direction matches the
    candidate's stance (additive only: never creates or removes a candidate)."""
    if tape is None or TAPE_SOURCE in c.sources:
        return c
    t = tape.tickers.get(c.ticker)
    if t is None or not tape_corroborates(c.stance.value, t.direction):
        return c
    return c.model_copy(update={"sources": [*c.sources, TAPE_SOURCE]})


def _mention(item: Any, batch: list[StoryPayload]) -> ScalpMention | None:
    """D56: the out-of-tier idea as a :class:`ScalpMention` (headline of its story)."""
    try:
        out = ScalpCandidateOut.model_validate(item)
    except ValidationError:
        return None
    ticker = normalize_ticker(out.ticker)
    cited = {s.strip() for s in out.sources}
    story = next((p for p in batch if cited & set(p.urls)), None) or next(
        (p for p in batch if ticker in {normalize_ticker(t) for t in p.tickers}), None
    )
    headline = " ".join((story.headline if story else "").split())
    return ScalpMention(
        ticker=ticker,
        stance=out.stance,
        catalyst_type=out.catalyst_type,
        headline=headline[:119] + "…" if len(headline) > 120 else headline,
    )


def _norm_ws(text: str) -> str:
    return " ".join(text.split()).lower()


def extractive_summary(story: Story, docs: dict[str, _Doc]) -> str:
    """No-LLM digest: the headline, else the first sentence of the first doc."""
    head = story.headline or headline_of(None, docs[story.docs[0].id].text)
    return (" ".join(head.split()) or "(no text)")[:600]


def story_payload(
    story: Story,
    docs: dict[str, _Doc],
    *,
    digest: Any | None = None,
) -> StoryPayload:
    """Build the stored digest; code-owned fields come from the cluster, never the LLM.

    Evidence quotes from the LLM are kept only when they occur (whitespace- and
    case-insensitively) in the text of a document of this story with that url.
    """
    texts = {docs[cd.id].url: _norm_ws(docs[cd.id].text) for cd in story.docs}
    evidence: list[StoryEvidence] = []
    summary = ""
    catalyst_type: CatalystType | None = None
    catalyst_date: str | None = None
    if digest is not None:
        summary = " ".join(str(digest.summary).split())[:600]
        catalyst_type = digest.catalyst_type
        parsed = parse_catalyst_date(digest.catalyst_date)
        catalyst_date = parsed.date().isoformat() if parsed else None
        for ev in digest.evidence:
            quote = " ".join(ev.quote.split())[:300]
            if quote and ev.url in texts and _norm_ws(quote) in texts[ev.url]:
                evidence.append(StoryEvidence(url=ev.url, quote=quote))
    return StoryPayload(
        story_id=story.id,
        headline=story.headline[:300],
        category=story.category,
        source_keys=story.source_keys,
        distinct_sources=story.distinct_sources,
        doc_ids=[cd.id for cd in story.docs],
        urls=story.urls[:50],
        first_published=story.first_published.isoformat(),
        last_published=story.last_published.isoformat(),
        tickers=story.tickers[:20],
        summary=summary or extractive_summary(story, docs),
        catalyst_type=catalyst_type,
        catalyst_date=catalyst_date,
        evidence=evidence[:3],
        mode="llm" if digest is not None and summary else "extractive",
    )


# ---------------------------------------------------------------------------
# Storage mapping
# ---------------------------------------------------------------------------


def _row_to_candidate(row: dict[str, Any]) -> Candidate:
    created = _dt.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=ET)
    return Candidate(
        id=row["id"],
        ticker=row["ticker"],
        stance=Stance(row["stance"]),
        catalyst_type=CatalystType(row["catalyst_type"]),
        catalyst_date=parse_catalyst_date(row["catalyst_date"]),
        confidence=row["confidence"],
        sources=json.loads(row["sources"]),
        corroboration=row.get("corroboration"),
        created_at=created.astimezone(ET),
    )


def count_corroboration(urls: Collection[str], source_key_of: Callable[[str], str]) -> int:
    """D30 rule (code, not the LLM): distinct registry sources behind *urls*.

    Ten URLs from one source count once; the LLM's own confidence or the number
    of URLs it cites never raises this number.
    """
    return len({source_key_of(u) for u in urls})


def store_candidate(
    repo: CandidateRepo,
    c: Candidate,
    *,
    day: str,
    run_id: str,
    source_key_of: Callable[[str], str] | None = None,
) -> Candidate:
    """Merge *c* into the stored row for ``(ticker, day)`` and persist it.

    With *source_key_of* (E4.5) the merged row's ``corroboration`` is recomputed
    from all of its sources, so repeated coverage by one source across runs still
    counts once.
    """
    existing = repo.get_for_day(c.ticker, day)
    merged = merge_candidates(_row_to_candidate(existing), c) if existing else c
    corroboration = (
        count_corroboration(merged.sources, source_key_of) if source_key_of is not None else None
    )
    merged = merged.model_copy(update={"corroboration": corroboration})
    row_id = repo.upsert_for_day(
        day=day,
        ticker=merged.ticker,
        stance=merged.stance.value,
        catalyst_type=merged.catalyst_type.value,
        catalyst_date=merged.catalyst_date.date().isoformat() if merged.catalyst_date else None,
        confidence=merged.confidence,
        sources=merged.sources,
        created_at=merged.created_at.isoformat(),
        run_id=run_id,
        corroboration=corroboration,
    )
    return merged.model_copy(update={"id": row_id})


def candidates_for_scanner(
    conn: sqlite3.Connection,
    day: str,
    *,
    min_confidence: float,
    floor_exempt: Collection[str] = (),
    tier_floors: Mapping[str, float] | None = None,
) -> list[Candidate]:
    """The ONLY Scalp output downstream stages may consume.

    Returns typed ``Candidate`` models (no persona free text) for *day*
    at or above *min_confidence*, best first. Tickers in *floor_exempt* (E12.4:
    core + momentum) are returned whatever their confidence.

    *tier_floors* (D56, ``ticker -> floor``) replaces both: only tier names at or
    above their own tier's floor are returned (a name in no tier never is).
    """
    if tier_floors is not None:
        floors = {normalize_ticker(t): f for t, f in tier_floors.items()}
        rows = CandidateRepo(conn).list_for_day(day, min_confidence=0.0)
        out: list[Candidate] = []
        for r in rows:
            t = normalize_ticker(r["ticker"])
            if t in floors and float(r["confidence"]) >= floors[t]:
                out.append(_row_to_candidate(r))
        return out
    exempt = {normalize_ticker(t) for t in floor_exempt}
    floor = 0.0 if exempt else min_confidence
    rows = CandidateRepo(conn).list_for_day(day, min_confidence=floor)
    return [
        _row_to_candidate(r)
        for r in rows
        if float(r["confidence"]) >= min_confidence or normalize_ticker(r["ticker"]) in exempt
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _parse_ts(raw: str | None) -> _dt.datetime:
    if not raw:
        return _dt.datetime.min.replace(tzinfo=ET)
    ts = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=ET)


def _load_docs(rows: list[dict[str, Any]], registry: SourceRegistry | None = None) -> list[_Doc]:
    reg = registry or SourceRegistry(sources={})
    out: list[_Doc] = []
    for r in rows:
        key = reg.key_for(r)
        out.append(
            _Doc(
                id=r["id"],
                source=r["source"],
                url=r["url"],
                published_at=r["published_at"],
                text=r["text"],
                tickers_hint=json.loads(r["tickers_hint"] or "[]"),
                title=r.get("title"),
                source_key=key,
                ingested_at=r.get("ingested_at") or "",
                category=reg.spec_for(key).category_key,
                feed=reg.spec_for(key).feed,
            )
        )
    return out


def load_fixture_docs(conn: sqlite3.Connection, path: Path | None = None) -> int:
    """Seed ``raw_docs`` from a fixture file (dry-run). Returns rows inserted."""
    path = path or FIXTURES_DIR / "raw_docs.json"
    repo = RawDocRepo(conn)
    inserted = 0
    for d in json.loads(path.read_text()):
        if repo.insert(
            source=d["source"],
            url=d["url"],
            published_at=d["published_at"],
            text=d["text"],
            tickers_hint=d.get("tickers_hint", []),
            id=d.get("id"),
            title=d.get("title"),
            source_key=d.get("source_key"),
        ):
            inserted += 1
    return inserted


def _default_registry() -> SourceRegistry:
    from arc.routines.config import load_routines

    try:
        return SourceRegistry.from_routines(load_routines())
    except (OSError, ValueError) as exc:  # pragma: no cover - broken config fails loudly elsewhere
        log.warning("scalp.registry_unavailable", error=str(exc))
        return SourceRegistry(sources={})


def _raw_doc_ttl(routines: RoutinesConfig | None) -> _dt.timedelta:
    """How long an unselected doc may wait before it is closed as ``skipped_budget``."""
    ttl = routines.context_policy("raw_doc_ref").ttl if routines is not None else None
    if ttl is not None and ttl.duration is not None:
        return ttl.duration
    if ttl is not None and ttl.sessions is not None:
        return _dt.timedelta(days=ttl.sessions)
    return _dt.timedelta(days=5)


def scalp_excluded(doc: _Doc) -> bool:
    """D45/D47/D49: the Scalp never reads this doc (YouTube, options data: typed context)."""
    return doc.category in {c.value for c in SCALP_EXCLUDED} or doc.category == LEGACY_VIDEO


def slow_feed(doc: _Doc) -> bool:
    """D54: the doc's source declares ``feed: scout`` (earnings calendar); never Scalp-read.

    D56: reference data (``reference: true``) is never Scalp-read either, whatever its feed.
    """
    return doc.feed == "scout" or doc.category == REFERENCE


def _doc_ts(raw: str) -> _dt.datetime | None:
    return _parse_ts(raw) if raw else None


def stale_docs(docs: list[_Doc], registry: SourceRegistry, now: _dt.datetime) -> list[_Doc]:
    """D47: docs older than their source's ``max_age`` (default: the category's) at *now*.

    Age is from ``published_at`` (EDGAR: the filing's accepted time), except sources
    with ``age_basis: ingested`` (earnings calendar rows: always the latest pull).
    """
    return [
        d
        for d in docs
        if registry.is_stale(
            d.source_key,
            published=_doc_ts(d.published_at),
            ingested=_doc_ts(d.ingested_at),
            now=now,
        )
    ]


def _select(
    docs: list[_Doc], registry: SourceRegistry, budget: int
) -> tuple[list[_Doc], list[_Doc], Selection]:
    docs = [d for d in docs if not (scalp_excluded(d) or slow_feed(d))]
    by_source: dict[str, list[_Doc]] = {}
    for d in sorted(docs, key=lambda d: (_parse_ts(d.published_at), d.id), reverse=True):
        by_source.setdefault(d.source_key, []).append(d)
    cat_of = {k: registry.spec_for(k).category for k in by_source}
    # D47: only categories that have docs this run share the budget; inside each,
    # the registry's per-source weights (normalised per category by select_fair).
    # D56: the budget splits equally across at most the two Scalp categories.
    present = {c for c in cat_of.values() if c is not None}
    cat_w = registry.category_weights(present)
    assert set(cat_w) <= SCALP_CATEGORIES and len(SCALP_CATEGORIES) == 2
    weights = registry.effective_weights(present)
    caps = {k: registry.spec_for(k).max_docs_per_run for k in by_source}
    # A category weighted 0 never reads (its docs wait, then close skipped_budget).
    readable = {k: [d.id for d in v] for k, v in by_source.items() if cat_of[k] in cat_w}
    sel = select_fair(
        readable,
        weights,
        budget,
        caps=caps,
        categories={k: registry.spec_for(k).category_key for k in readable},
        category_weights={c.value: w for c, w in cat_w.items()},
    )
    if len(readable) != len(by_source):  # zero-weight categories: available, never picked
        sel = Selection(
            selected=sel.selected,
            picked=sel.picked,
            available={k: len(v) for k, v in by_source.items()},
            weights=sel.weights,
            category_of={
                **sel.category_of,
                **{k: registry.spec_for(k).category_key for k in by_source},
            },
            category_weights=sel.category_weights,
        )
    chosen = set(sel.selected)
    selected = [d for d in docs if d.id in chosen]
    unselected = [d for d in docs if d.id not in chosen]
    return selected, unselected, sel


def select_docs(
    docs: list[_Doc], registry: SourceRegistry, budget: int
) -> tuple[list[_Doc], list[_Doc], list[tuple[str, int, int]]]:
    """D30/D47 fair pick: ``(selected, unselected, source_mix)``; newest first per source.

    The budget is split equally across categories that have docs (D47), then across
    the sources inside each category. Docs in a :data:`SCALP_EXCLUDED` category are
    never selected and are not counted as unselected either (the caller closes them
    as brief-only). Callers drop stale docs first (:func:`stale_docs`).
    """
    selected, unselected, sel = _select(docs, registry, budget)
    return selected, unselected, format_source_mix(sel, registry)


def _cluster(docs: list[_Doc], settings: ArcSettings) -> list[Story]:
    cdocs = [
        ClusterDoc(
            id=d.id,
            source_key=d.source_key,
            source=d.source,
            url=d.url,
            published_at=_parse_ts(d.published_at),
            headline=headline_of(d.title, d.text),
            tickers=tuple(d.tickers_hint),
            category=d.category,
            form_type=form_type_of(d.title, d.url, d.text) if d.source == "edgar" else None,
        )
        for d in docs
    ]
    return cluster_stories(
        cdocs,
        threshold=settings.scalp_story_threshold,
        window=_dt.timedelta(hours=settings.scalp_story_window_hours),
    )


def _digest_stories(
    stories: list[Story],
    docs: dict[str, _Doc],
    *,
    digest_llm: PersonaLLM | None,
    settings: ArcSettings,
    day: str,
    run_id: str,
    batch_repo: ScalpBatchRepo,
    result: ScalpRunResult,
) -> list[StoryPayload]:
    """Stage 1: one digest per story, batched per source category (cheap tier).

    Without *digest_llm* (dry-run / fixtures) or when a batch fails, stories get an
    extractive digest (headline) so the Scalp still sees them; failures are audited.
    """
    out: dict[str, StoryPayload] = {}
    if digest_llm is None:
        return [story_payload(s, docs) for s in stories]
    by_cat: dict[str, list[Story]] = {}
    for s in stories:
        by_cat.setdefault(s.category, []).append(s)
    size = settings.scalp_batch_size
    for cat in sorted(by_cat):
        group = by_cat[cat]
        for i in range(0, len(group), size):
            batch = group[i : i + size]
            doc_ids = [cd.id for s in batch for cd in s.docs]
            prompt = build_digest_prompt(
                [render_story(s, docs, max_chars=settings.scalp_story_doc_chars) for s in batch],
                day,
            )
            result.digest_batches += 1
            try:
                reply = digest_llm.complete(prompt)
            except ScalpLLMError as exc:
                result.failed_digest_batches += 1
                batch_repo.insert(
                    run_id=run_id,
                    model=getattr(digest_llm, "model", "unknown"),
                    doc_ids=doc_ids,
                    prompt=prompt,
                    raw_response=None,
                    status="llm_error",
                    error=str(exc),
                    stage="digest",
                )
                log.warning("scalp.digest.llm_error", run_id=run_id, error=str(exc))
                continue
            result.add_usage(reply)
            try:
                parsed = StoryDigestOutput.model_validate(extract_json_object(reply.text))
            except (ValueError, ValidationError) as exc:
                result.failed_digest_batches += 1
                batch_repo.insert(
                    run_id=run_id,
                    model=reply.model,
                    doc_ids=doc_ids,
                    prompt=prompt,
                    raw_response=reply.text,
                    status="parse_error",
                    error=str(exc)[:500],
                    stage="digest",
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                    cost_usd=reply.cost_usd,
                )
                log.warning("scalp.digest.parse_error", run_id=run_id, error=str(exc)[:200])
                continue
            wanted = {s.id: s for s in batch}
            for d in parsed.stories:
                story = wanted.get(d.story_id.strip())
                if story is not None and story.id not in out:
                    out[story.id] = story_payload(story, docs, digest=d)
            batch_repo.insert(
                run_id=run_id,
                model=reply.model,
                doc_ids=doc_ids,
                prompt=prompt,
                raw_response=reply.text,
                status="ok",
                accepted=sum(1 for s in batch if s.id in out),
                stage="digest",
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                cost_usd=reply.cost_usd,
            )
    # Stories the LLM skipped or whose batch failed: extractive fallback.
    return [out.get(s.id) or story_payload(s, docs) for s in stories]


def run_scalp(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    llm: PersonaLLM | None = None,
    digest_llm: PersonaLLM | None = None,
    dry_run: bool = False,
    now: _dt.datetime | None = None,
    run_id: str | None = None,
    guard: UniverseGuard | None = None,
    routines: RoutinesConfig | None = None,
    registry: SourceRegistry | None = None,
    on_story: Callable[[StoryPayload], None] | None = None,
) -> ScalpRunResult:
    """Fair-select unscalped docs, cluster them into stories, digest, then scalp (D30).

    1. **Select** (:func:`select_docs`): docs older than their category's ``max_age``
       are closed ``skipped_stale`` (D47). ``scalp_doc_budget`` docs are then split
       equally across categories that have fresh docs, then by weighted round-robin
       across the sources inside each, newest first per source. Docs left over wait
       for the next run; once older than the ``raw_doc_ref`` context TTL they are
       closed as ``skipped_budget`` with this run id.
    2. **Cluster** (:mod:`arc.ingest.stories`) near-duplicates into stories.
    3. **Stage 1** (*digest_llm*, cheap tier): one short digest per story, batched by
       category. Default: the Scalp backend when live; extractive (no LLM) in
       dry-run or when *llm* is injected without a *digest_llm*.
    4. **Stage 2** (*llm*): the Scalp reads digests, ``scalp_story_batch_size`` per
       call; candidates are validated as before, and ``corroboration`` is set by
       code from distinct sources (:func:`count_corroboration`).

    *on_story* is called with every digest (the job writes it as a ``story`` context
    entry). ``dry_run=True`` uses the canned fixture responses and never calls the
    network. *guard* (D28) overrides the universe policy built from *settings*.
    """
    now = (now or now_et()).astimezone(ET)
    day = now.date().isoformat()
    run_id = run_id or f"scalp-{uuid.uuid4().hex[:12]}"
    if llm is None:
        if dry_run:
            llm = FixtureScalpLLM.from_dir(FIXTURES_DIR / "responses")
        else:
            llm = HermesScalpLLM.from_settings(settings)
            digest_llm = digest_llm or llm
    if registry is None:
        registry = (
            SourceRegistry.from_routines(routines) if routines is not None else _default_registry()
        )

    result = ScalpRunResult(run_id=run_id, day=day, dry_run=dry_run)
    doc_repo = RawDocRepo(conn)
    batch_repo = ScalpBatchRepo(conn)
    cand_repo = CandidateRepo(conn)

    # 1. fair selection
    all_docs = _load_docs(doc_repo.list_unscalped(limit=None), registry)
    brief_only = [d.id for d in all_docs if scalp_excluded(d)]
    if brief_only:  # D45: video docs wait for nobody; close them out of the queue
        doc_repo.mark_brief_only(brief_only, run_id=run_id)
        all_docs = [d for d in all_docs if not scalp_excluded(d)]
    # D54: slow-feed docs (earnings calendar) stay stored for next_earnings() / the Scout
    # but never draw on scalp_doc_budget; close them out of the Scalp's queue.
    slow = [d.id for d in all_docs if slow_feed(d)]
    if slow:
        doc_repo.mark_slow_feed(slow, run_id=run_id)
        result.slow_feed = len(slow)
        all_docs = [d for d in all_docs if not slow_feed(d)]
    # D55: title-filtered docs were closed at insert; claim and count them (never read).
    result.filtered_by_source = doc_repo.claim_filtered(run_id=run_id)
    result.filtered = sum(result.filtered_by_source.values())
    # D47: a doc past its category's max_age is never read; close it skipped_stale.
    stale = stale_docs(all_docs, registry, now)
    if stale:
        doc_repo.mark_skipped_stale([d.id for d in stale], run_id=run_id)
        result.skipped_stale = len(stale)
        result.stale_by_source = dict(Counter(d.source_key for d in stale))
        stale_ids = {d.id for d in stale}
        all_docs = [d for d in all_docs if d.id not in stale_ids]
    selected, unselected, selection = _select(all_docs, registry, settings.scalp_doc_budget)
    result.source_mix = format_source_mix(selection, registry)
    result.category_mix = category_mix(selection, registry, stale=result.stale_by_source)
    result.over_budget = len(unselected)
    ttl = _raw_doc_ttl(routines)
    expired = [d.id for d in unselected if _parse_ts(d.ingested_at or d.published_at) + ttl <= now]
    if expired:
        doc_repo.mark_skipped_budget(expired, run_id=run_id)
        result.skipped_budget = len(expired)
    docs = {d.id: d for d in selected}
    key_by_url = {d.url: d.source_key for d in all_docs}

    def source_key_of(url: str) -> str:
        if url == TAPE_SOURCE:  # E13.10: the tape is its own source (never a doc)
            return TAPE_SOURCE
        if url in key_by_url:
            return key_by_url[url]
        row = doc_repo.source_keys_for_urls([url]).get(url)
        return registry.key_for(row) if row else url

    if guard is None:
        guard = UniverseGuard.from_settings(settings, now=now, load_master=bool(docs), conn=conn)
    open_universe = guard.mode == "seed"
    watch = watch_tickers(conn, settings, now)  # D51: core + momentum + trending
    log.info(
        "scalp.run.start",
        run_id=run_id,
        day=day,
        unscalped=len(all_docs),
        selected=len(selected),
        over_budget=result.over_budget,
        skipped_budget=result.skipped_budget,
        skipped_stale=result.skipped_stale,
        filtered=result.filtered,
        dry_run=dry_run,
    )

    # 2. cluster, 3. stage-1 digests
    stories = _cluster(selected, settings)
    digests = _digest_stories(
        stories,
        docs,
        digest_llm=digest_llm,
        settings=settings,
        day=day,
        run_id=run_id,
        batch_repo=batch_repo,
        result=result,
    )
    result.stories = digests
    for p in digests:
        ttl = registry.freshness_ttl(p.source_keys, None, now)
        if ttl is not None:
            result.story_ttls[p.story_id] = ttl
        if on_story is not None:
            on_story(p)

    # 4. stage 2: the Scalp reads digests (E4.8a: + Finnhub facts when the flag is on)
    facts_snap = _facts_snapshot(conn, routines, now, run_id) if digests else None
    tape = _options_tape(conn, settings, routines, now, run_id) if digests else None
    result.tape = tape
    size = settings.scalp_story_batch_size
    for i in range(0, len(digests), size):
        batch = digests[i : i + size]
        doc_ids = [d for p in batch for d in p.doc_ids]
        facts = ""
        if facts_snap is not None and routines is not None:
            cfg = routines.finnhub_context
            tickers = scalp_facts_tickers(batch, cfg.scalp_max_tickers)
            facts = ticker_facts_block(
                facts_snap, cfg.prompt_options(tickers, cfg.scalp_max_tickers)
            )
        prompt = build_stage2_prompt(
            batch,
            settings,
            day,
            open_universe=open_universe,
            ticker_facts=facts,
            universe=watch,
            tape=tape.text if tape is not None else None,
        )
        result.batches += 1

        try:
            reply = llm.complete(prompt)
        except ScalpLLMError as exc:
            # Docs stay unscalped so the next run retries them.
            result.failed_batches += 1
            batch_repo.insert(
                run_id=run_id,
                model=getattr(llm, "model", "unknown"),
                doc_ids=doc_ids,
                prompt=prompt,
                raw_response=None,
                status="llm_error",
                error=str(exc),
            )
            log.warning("scalp.batch.llm_error", run_id=run_id, error=str(exc))
            continue
        result.add_usage(reply)
        usage = {
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cost_usd": reply.cost_usd,
        }

        try:
            payload = extract_json_object(reply.text)
            items = payload["candidates"]
            if not isinstance(items, list):
                raise TypeError("candidates is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            result.failed_batches += 1
            batch_repo.insert(
                run_id=run_id,
                model=reply.model,
                doc_ids=doc_ids,
                prompt=prompt,
                raw_response=reply.text,
                status="parse_error",
                error=str(exc),
                **usage,
            )
            log.warning("scalp.batch.parse_error", run_id=run_id, error=str(exc))
            continue

        allowed_sources = frozenset(u for p in batch for u in p.urls)
        summary = payload.get("scan_summary") if isinstance(payload, dict) else None
        if isinstance(summary, str) and summary.strip():
            result.summaries.append(summary.strip())
            result.summary_sources.extend(u for p in batch for u in p.urls if u)
        rejected: Counter[str] = Counter()
        accepted = 0
        for item in items:
            outcome = validate_scalp_candidate(
                item,
                universe=guard,
                min_confidence=settings.scalp_min_confidence,
                allowed_sources=allowed_sources,
                created_at=now,
            )
            if isinstance(outcome, str):
                rejected[outcome] += 1
                raw = item.get("ticker") if isinstance(item, dict) else None
                label = normalize_ticker(str(raw or "?"))[:12] or "?"
                result.rejected_items.setdefault(outcome, []).append(label)
                if outcome == REJECT_THRESHOLD and guard.model == "d56":
                    _note_floor_reject(result, guard, label, item)
                if (
                    outcome == REJECT_NOT_IN_TIER
                    and len(result.mentions) < MAX_MENTIONS
                    and label not in {m.ticker for m in result.mentions}
                    and (m := _mention(item, batch)) is not None
                ):
                    result.mentions.append(m)
                if label in guard.details:
                    result.reject_details[label] = guard.details[label]
                continue
            tagged = with_tape_source(outcome, tape)
            if tagged is not outcome and tape is not None:
                result.tape_corroborated[outcome.ticker] = tape.tickers[outcome.ticker].pc_volume
            outcome = tagged
            store_candidate(cand_repo, outcome, day=day, run_id=run_id, source_key_of=source_key_of)
            accepted += 1
            why = item.get("rationale") if isinstance(item, dict) else None
            best = result._rationale_conf.get(outcome.ticker, -1.0)
            if isinstance(why, str) and why.strip() and outcome.confidence >= best:
                result._rationale_conf[outcome.ticker] = outcome.confidence
                result.rationales[outcome.ticker] = " ".join(why.split())[:240]

        batch_repo.insert(
            run_id=run_id,
            model=reply.model,
            doc_ids=doc_ids,
            prompt=prompt,
            raw_response=reply.text,
            status="ok",
            accepted=accepted,
            rejected=dict(rejected),
            **usage,
        )
        doc_repo.mark_scalped(doc_ids, run_id=run_id)
        result.docs_scalped += len(doc_ids)
        result.accepted += accepted
        result.rejected.update(rejected)
        log.info(
            "scalp.batch.ok",
            run_id=run_id,
            stories=len(batch),
            docs=len(doc_ids),
            accepted=accepted,
            rejected=dict(rejected),
        )

    result.new_tickers = list(guard.admitted_new)
    result.candidates = candidates_for_scanner(
        conn,
        day,
        min_confidence=settings.scalp_min_confidence,
        floor_exempt=guard.floor_exempt() if guard.model != "d56" else (),
        tier_floors=guard.tier_floors() if guard.model == "d56" else None,
    )
    # E12.4: day-level candidates below the floor that stayed because of their tier
    for c in result.candidates if guard.model != "d56" else ():
        tier = guard.skips_confidence_floor(c.ticker)
        if c.confidence < settings.scalp_min_confidence and tier is not None:
            result.floor_skipped[c.ticker] = (tier.value, c.confidence)
    for cand in result.candidates:
        # E13.10: the tape token is not a doc source; it never sets a freshness TTL.
        keys = [source_key_of(u) for u in cand.sources if u != TAPE_SOURCE]
        ttl = registry.freshness_ttl(keys, None, now)
        if ttl is not None:
            result.candidate_ttls[cand.ticker] = ttl
    log.info(
        "scalp.run.done",
        run_id=run_id,
        batches=result.batches,
        failed=result.failed_batches,
        digest_batches=result.digest_batches,
        failed_digest=result.failed_digest_batches,
        stories=len(result.stories),
        accepted=result.accepted,
        candidates=len(result.candidates),
        universe_mode=str(guard.mode),
        new_tickers=result.new_tickers,
        mentions=[m.ticker for m in result.mentions],
        tape_present=result.tape_present,
        tape_corroborated=sorted(result.tape_corroborated),
        rejected=dict(result.rejected),
        source_mix=result.source_mix,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
    return result


# D56 (E13.1): pre-rename names, re-exported for one release.
SweepRunResult = ScalpRunResult
run_sweep = run_scalp
