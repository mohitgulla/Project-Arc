"""Scout candidate pipeline (E4.2): RawDoc batches → validated ``Candidate`` rows.

Flow per run::

    raw_docs (unscouted) ──batch──▶ Scout prompt ──Hermes (cheap tier)──▶ raw JSON
        ──▶ ScoutOutput schema ──▶ per-candidate filters ──▶ merge per ticker/day
        ──▶ candidates table                       (+ scout_batches audit row)

Filters (deterministic, applied after the LLM):

* **schema** — each candidate must validate against ``ScoutCandidateOut``.
* **universe** (D28, :class:`~arc.universe.guard.UniverseGuard`) — ``strict``:
  ticker must be in ``settings.universe``. ``seed`` (default): seed tickers
  always pass; any other ticker must be in the symbol master
  (``unknown_symbol``), optionable, under the per-run new-ticker cap
  (``over_new_ticker_cap``) and pass the liquidity screen (``illiquid``).
* **threshold** — confidence must be ``>= settings.scout_min_confidence``.
* **sources** — only URLs of documents actually in the batch survive; a
  candidate with no grounded source is dropped (no hallucinated citations).

The liquidity screen runs last (after threshold and sources), so market data is
only fetched for candidates that would otherwise be accepted.

Funnel discipline: the only thing downstream code (scanner, Director) may
read is :func:`candidates_for_scanner`, which returns ``Candidate`` models
— enums, symbols, numbers, dates and source URLs. Persona free text
(rationale, scan summary, the verbatim response) is stored in
``scout_batches`` for audit and never leaves it.
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
from pydantic import ValidationError

from arc.ingest.llm import FixtureScoutLLM, HermesScoutLLM, ScoutLLMError
from arc.ingest.store import RawDocRepo, ScoutBatchRepo
from arc.models import Candidate, CatalystType, Stance
from arc.personas.builders import ScoutInput, build_scout_prompt
from arc.personas.schemas import ScoutCandidateOut, ScoutOutput
from arc.store.repos import CandidateRepo
from arc.universe.guard import (
    REJECT_ILLIQUID,
    REJECT_NEW_TICKER_CAP,
    REJECT_NOT_IN_UNIVERSE,
    REJECT_UNKNOWN_SYMBOL,
    UniverseGuard,
)
from arc.utils.calendar import ET, now_et

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection

    from arc.config import ArcSettings
    from arc.ingest.llm import ScoutLLM

log = structlog.get_logger()

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "scout"

# Rejection reasons (stable keys; stored in scout_batches.rejected).
REJECT_SCHEMA = "schema"
REJECT_UNIVERSE = REJECT_NOT_IN_UNIVERSE  # strict mode (kept name for callers)
REJECT_THRESHOLD = "below_threshold"
REJECT_SOURCE = "no_grounded_source"
# Re-exported for callers/tests (the universe keys live in arc.universe.guard).
_UNIVERSE_REJECTS = (REJECT_ILLIQUID, REJECT_NEW_TICKER_CAP, REJECT_UNKNOWN_SYMBOL)

_FEED_DELIMITER = "FEEDS>>>"
_MAX_UNSCOUTED_PER_RUN = 200


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ScoutRunResult:
    """Summary of a Scout run. ``candidates`` is the post-merge state for the day."""

    run_id: str
    day: str
    dry_run: bool
    batches: int = 0
    failed_batches: int = 0
    docs_scouted: int = 0
    accepted: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    rejected_items: dict[str, list[str]] = field(default_factory=dict)  # reason -> tickers
    candidates: list[Candidate] = field(default_factory=list)
    # ticker -> one-line Scout rationale (highest-confidence accepted item this run).
    # Display only (Slack digest); never copied onto ``Candidate`` (funnel discipline).
    rationales: dict[str, str] = field(default_factory=dict)
    # D27: each ok batch's ``scan_summary`` (+ the doc URLs it covered), written by the
    # scout job as one ``note`` (topic=observation). Never copied onto ``Candidate``.
    summaries: list[str] = field(default_factory=list)
    summary_sources: list[str] = field(default_factory=list)
    # D28: ticker -> why the universe guard rejected it (screen failures etc.), and the
    # non-seed tickers admitted this run. Display + journal only.
    reject_details: dict[str, str] = field(default_factory=dict)
    new_tickers: list[str] = field(default_factory=list)
    _rationale_conf: dict[str, float] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class _Doc:
    id: str
    source: str
    url: str
    published_at: str
    text: str
    tickers_hint: list[str]


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


def validate_scout_candidate(
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
    and liquidity screen run last, only for otherwise-valid candidates.
    """
    try:
        out = ScoutCandidateOut.model_validate(item)
    except ValidationError:
        return REJECT_SCHEMA

    ticker = normalize_ticker(out.ticker)
    guard = universe if isinstance(universe, UniverseGuard) else None
    if guard is not None:
        if (why := guard.known(ticker)) is not None:
            return why
    elif ticker not in cast("Collection[str]", universe):
        return REJECT_UNIVERSE
    if out.confidence < min_confidence:
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


def render_doc(doc: _Doc, *, max_chars: int) -> str:
    """Render one RawDoc for the Scout prompt (truncated, delimiter-safe)."""
    text = doc.text.replace(_FEED_DELIMITER, "FEEDS>")
    if len(text) > max_chars:
        text = text[:max_chars] + " …[truncated]"
    hints = ",".join(doc.tickers_hint) or "-"
    return (
        f"[doc {doc.id}] source={doc.source} url={doc.url} "
        f"published={doc.published_at} tickers_hint={hints}\n{text}"
    )


def build_prompt(
    docs: list[_Doc], settings: ArcSettings, day: str, *, open_universe: bool | None = None
) -> str:
    if open_universe is None:
        open_universe = settings.universe_mode == "seed"
    return build_scout_prompt(
        ScoutInput(
            universe=list(settings.universe),
            raw_feeds=[render_doc(d, max_chars=settings.scout_max_doc_chars) for d in docs],
            scan_date=day,
            min_confidence=settings.scout_min_confidence,
            output_schema_json=json.dumps(ScoutOutput.model_json_schema(), sort_keys=True),
            open_universe=open_universe,
        )
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
        created_at=created.astimezone(ET),
    )


def store_candidate(repo: CandidateRepo, c: Candidate, *, day: str, run_id: str) -> Candidate:
    """Merge *c* into the stored row for ``(ticker, day)`` and persist it."""
    existing = repo.get_for_day(c.ticker, day)
    merged = merge_candidates(_row_to_candidate(existing), c) if existing else c
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
    )
    return merged.model_copy(update={"id": row_id})


def candidates_for_scanner(
    conn: sqlite3.Connection, day: str, *, min_confidence: float
) -> list[Candidate]:
    """The ONLY Scout output downstream stages may consume.

    Returns typed ``Candidate`` models (no persona free text) for *day*
    at or above *min_confidence*, best first.
    """
    rows = CandidateRepo(conn).list_for_day(day, min_confidence=min_confidence)
    return [_row_to_candidate(r) for r in rows]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _load_docs(rows: list[dict[str, Any]]) -> list[_Doc]:
    return [
        _Doc(
            id=r["id"],
            source=r["source"],
            url=r["url"],
            published_at=r["published_at"],
            text=r["text"],
            tickers_hint=json.loads(r["tickers_hint"] or "[]"),
        )
        for r in rows
    ]


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
        ):
            inserted += 1
    return inserted


def run_scout(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    *,
    llm: ScoutLLM | None = None,
    dry_run: bool = False,
    now: _dt.datetime | None = None,
    run_id: str | None = None,
    guard: UniverseGuard | None = None,
) -> ScoutRunResult:
    """Summarise all unscouted RawDocs into stored, merged ``Candidate`` rows.

    ``dry_run=True`` swaps the Hermes backend for canned fixture responses
    (``arc/ingest/fixtures/scout/responses``) unless *llm* is given; it never
    makes a network call. *guard* (D28) overrides the universe policy built from
    *settings* (tests and ``arc propose --fixtures`` pass one with recorded data).
    """
    now = (now or now_et()).astimezone(ET)
    day = now.date().isoformat()
    run_id = run_id or f"scout-{uuid.uuid4().hex[:12]}"
    if llm is None:
        llm = (
            FixtureScoutLLM.from_dir(FIXTURES_DIR / "responses")
            if dry_run
            else HermesScoutLLM.from_settings(settings)
        )

    result = ScoutRunResult(run_id=run_id, day=day, dry_run=dry_run)
    doc_repo = RawDocRepo(conn)
    batch_repo = ScoutBatchRepo(conn)
    cand_repo = CandidateRepo(conn)
    docs = _load_docs(doc_repo.list_unscouted(limit=_MAX_UNSCOUTED_PER_RUN))
    if guard is None:
        guard = UniverseGuard.from_settings(settings, now=now, load_master=bool(docs))
    open_universe = guard.mode == "seed"
    size = settings.scout_batch_size
    log.info("scout.run.start", run_id=run_id, day=day, docs=len(docs), dry_run=dry_run)

    for i in range(0, len(docs), size):
        batch = docs[i : i + size]
        doc_ids = [d.id for d in batch]
        prompt = build_prompt(batch, settings, day, open_universe=open_universe)
        result.batches += 1

        try:
            reply = llm.complete(prompt)
        except ScoutLLMError as exc:
            # Docs stay unscouted so the next run retries them.
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
            log.warning("scout.batch.llm_error", run_id=run_id, error=str(exc))
            continue

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
            )
            log.warning("scout.batch.parse_error", run_id=run_id, error=str(exc))
            continue

        allowed_sources = frozenset(d.url for d in batch)
        summary = payload.get("scan_summary") if isinstance(payload, dict) else None
        if isinstance(summary, str) and summary.strip():
            result.summaries.append(summary.strip())
            result.summary_sources.extend(d.url for d in batch if d.url)
        rejected: Counter[str] = Counter()
        accepted = 0
        for item in items:
            outcome = validate_scout_candidate(
                item,
                universe=guard,
                min_confidence=settings.scout_min_confidence,
                allowed_sources=allowed_sources,
                created_at=now,
            )
            if isinstance(outcome, str):
                rejected[outcome] += 1
                raw = item.get("ticker") if isinstance(item, dict) else None
                label = normalize_ticker(str(raw or "?"))[:12] or "?"
                result.rejected_items.setdefault(outcome, []).append(label)
                if label in guard.details:
                    result.reject_details[label] = guard.details[label]
                continue
            store_candidate(cand_repo, outcome, day=day, run_id=run_id)
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
        )
        doc_repo.mark_scouted(doc_ids, run_id=run_id)
        result.docs_scouted += len(batch)
        result.accepted += accepted
        result.rejected.update(rejected)
        log.info(
            "scout.batch.ok",
            run_id=run_id,
            docs=len(batch),
            accepted=accepted,
            rejected=dict(rejected),
        )

    result.new_tickers = list(guard.admitted_new)
    result.candidates = candidates_for_scanner(
        conn, day, min_confidence=settings.scout_min_confidence
    )
    log.info(
        "scout.run.done",
        run_id=run_id,
        batches=result.batches,
        failed=result.failed_batches,
        accepted=result.accepted,
        candidates=len(result.candidates),
        universe_mode=str(guard.mode),
        new_tickers=result.new_tickers,
        rejected=dict(result.rejected),
    )
    return result
