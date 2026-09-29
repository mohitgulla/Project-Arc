"""Handler contract for routine jobs and the built-in source/persona handlers.

A handler is ``Callable[[JobContext], JobResult]``. It reads what it needs from
``ctx.snapshot`` (already recorded on the run) and writes every output through
``ctx.write`` so the entry ids land in ``routine_runs.outputs``. Handlers never
hand results to each other in memory (D16).

Resolution order for a job/step name (see :func:`resolve_handler`):

1. ``handler: "package.module:function"`` in routines.yaml,
2. an exact entry in :data:`BUILTIN_HANDLERS`,
3. the name's first dotted segment (``youtube.stockedup`` -> ``youtube``), so a
   new channel is a YAML-only change,
4. otherwise :func:`not_implemented`, which records the run as ``skipped``
   (persona handlers owned by later cards: E6.x Investor, Auditor).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.store import ContextEntry, ContextSnapshot, ContextStore

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Mapping

    from pydantic import BaseModel

    from arc.config import ArcSettings
    from arc.ingest.llm import ScoutLLM
    from arc.ingest.scout import ScoutRunResult
    from arc.models import RawDoc
    from arc.routines.config import JobKind, RoutinesConfig, StepSpec
    from arc.routines.manifest import ExternalInput
    from arc.routines.runs import RoutineEvent
    from arc.slack.blocks import CardView

log = structlog.get_logger(__name__)


class JobSkippedError(Exception):
    """Raised by a handler to record its run as ``skipped`` (not a failure)."""


class ContractViolationError(RuntimeError):
    """A job wrote a context kind outside its declared ``writes`` (D27, fail-closed).

    Not caught by handlers: the dispatcher records the run as ``failed`` and alerts.
    """


@dataclass
class JobResult:
    """What a handler reports back. ``metrics`` feed trigger conditions.

    ``summary`` is the one-line heartbeat (and the notification fallback text);
    ``card`` is the optional E5.5 digest card posted under ``notify: card``.
    ``notice`` (optional) is posted to the day thread immediately, even for a
    quiet job: use it for things a human must see now (e.g. a daily-loss halt).
    """

    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    card: CardView | None = None
    notice: str = ""


@dataclass
class JobContext:
    """Everything a handler may use. Created by the dispatcher per run."""

    job: str
    kind: JobKind
    spec: StepSpec
    run_id: str
    chain_run_id: str | None
    scheduled_for: _dt.datetime
    now: _dt.datetime
    conn: sqlite3.Connection
    snapshot: ContextSnapshot
    routines: RoutinesConfig
    event: RoutineEvent | None = None
    settings_factory: Callable[[], ArcSettings] | None = None
    outputs: list[str] = field(default_factory=list)
    external_inputs: list[ExternalInput] = field(default_factory=list)
    _settings: ArcSettings | None = None
    # Wall clock for steps that judge data age (E5.2b). ``now`` is the tick/chain
    # start time and stays the idempotency key; ``clock()`` is "now, really".
    clock_fn: Callable[[], _dt.datetime] | None = None

    @property
    def clock(self) -> Callable[[], _dt.datetime]:
        """Fresh time source; defaults to the frozen ``now`` (tests, replays, ``--now``)."""
        if self.clock_fn is not None:
            return self.clock_fn
        return lambda: self.now

    @property
    def settings(self) -> ArcSettings:
        if self._settings is None:
            if self.settings_factory is not None:
                self._settings = self.settings_factory()
            else:
                from arc.control.effective import effective_settings

                self._settings = effective_settings(self.conn)  # D26 overrides
        return self._settings

    @property
    def options(self) -> dict[str, Any]:
        return self.spec.options

    def record_input(
        self,
        name: str,
        source: str,
        payload: object,
        *,
        as_of: _dt.datetime | None = None,
        count: int | None = None,
    ) -> ExternalInput:
        """Record market/broker/DB data this run used (D27 run manifest).

        Only the sha256 of *payload*'s canonical JSON is kept, never the data.
        """
        from arc.routines.manifest import ExternalInput, digest

        item = ExternalInput(
            name=name, source=source, as_of=as_of, digest=digest(payload), count=count
        )
        self.external_inputs.append(item)
        return item

    def write(
        self,
        kind: str,
        subject: str,
        payload: BaseModel | Mapping[str, object],
        *,
        valid_from: _dt.datetime | None = None,
    ) -> ContextEntry:
        """Append a context entry using this job's TTL/supersede policy.

        Raises :class:`ContractViolationError` (nothing is written) when *kind*
        is not in the job's declared ``writes`` (D27, fail-closed).
        """
        declared = list(self.spec.writes or [])
        if kind not in declared:
            log.error("context.write_rejected", job=self.job, kind=kind, declared=declared)
            msg = f"job {self.job!r} wrote kind {kind!r} not in its declared writes {declared}"
            raise ContractViolationError(msg)
        policy = self.routines.context_policy(kind, self.job)
        entry = ContextStore(self.conn).write(
            kind=kind,
            subject=subject,
            payload=payload,
            produced_by=self.job,
            ttl=policy.ttl,
            supersede=policy.supersede,
            run_id=self.run_id,
            chain_run_id=self.chain_run_id,
            valid_from=valid_from,
            now=self.now,
        )
        self.outputs.append(entry.id)
        return entry


Handler = Callable[[JobContext], JobResult]


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


def not_implemented(ctx: JobContext) -> JobResult:
    """Placeholder for persona steps whose runner lands in a later card."""
    msg = f"no handler registered for {ctx.job!r} yet"
    raise JobSkippedError(msg)


def _doc_refs(ctx: JobContext, docs: list[RawDoc]) -> int:
    from arc.context.kinds import RawDocRefPayload

    written = 0
    for doc in docs:
        row = ctx.conn.execute(
            "SELECT id FROM raw_docs WHERE content_hash = ?", (doc.content_hash,)
        ).fetchone()
        if row is None:  # pragma: no cover - fetchers return stored docs only
            continue
        # One subject per document: refs from the same job must not supersede
        # each other. The producing job is recorded in ``produced_by``.
        ctx.write(
            "raw_doc_ref",
            row["id"],
            RawDocRefPayload(
                doc_id=row["id"],
                source=doc.source,
                url=doc.url,
                published_at=doc.published_at.isoformat(),
            ),
        )
        written += 1
    return written


def _source_result(ctx: JobContext, docs: list[RawDoc]) -> JobResult:
    ctx.record_input(
        "raw_docs",
        ctx.job.split(".", 1)[0],  # upstream source (rss, edgar, youtube, ...)
        sorted(d.content_hash for d in docs),
        as_of=ctx.now,
        count=len(docs),
    )
    n = _doc_refs(ctx, docs)
    return JobResult(summary=f"{n} new doc{'s' if n != 1 else ''}", metrics={"new_docs": n})


def rss_source(ctx: JobContext) -> JobResult:
    from arc.ingest.rss import fetch_rss

    settings = ctx.settings
    feeds = ctx.options.get("feeds")
    if feeds:
        settings = settings.model_copy(update={"ingest_rss_feeds": list(feeds)})
    return _source_result(ctx, fetch_rss(ctx.conn, settings))


def edgar_source(ctx: JobContext) -> JobResult:
    from arc.ingest.edgar import fetch_edgar

    settings = ctx.settings
    tickers = ctx.options.get("tickers")
    if tickers:
        settings = settings.model_copy(update={"universe": list(tickers)})
    return _source_result(ctx, fetch_edgar(ctx.conn, settings))


def earnings_source(ctx: JobContext) -> JobResult:
    from arc.ingest.earnings import fetch_earnings

    return _source_result(ctx, fetch_earnings(ctx.conn, ctx.settings))


def youtube_url(channel: str) -> str:
    """Accept a full URL or a bare ``UC...`` channel id."""
    if channel.startswith(("http://", "https://")):
        return channel
    return f"https://www.youtube.com/channel/{channel}/videos"


def youtube_source(ctx: JobContext) -> JobResult:
    """One YouTube channel per job (``channel:`` option), else the configured list.

    The summary carries the run's caption outcome (ok / rate_limited / empty /
    error / skipped by breaker or cooldown), audio fallbacks with wall time, and
    the current ``youtube:captions_backoff`` cooldown, so every scheduled run
    shows how the E4.1c backoff behaved.
    """
    from arc.ingest.youtube import YoutubeRunStats, fetch_youtube

    settings = ctx.settings
    channel = ctx.options.get("channel")
    if channel:
        settings = settings.model_copy(update={"ingest_youtube_channels": [youtube_url(channel)]})
    stats = YoutubeRunStats()
    result = _source_result(ctx, fetch_youtube(ctx.conn, settings, stats=stats))
    result.summary = f"{result.summary} · {stats.summary()}"
    result.metrics.update(
        {
            "captions_ok": stats.captions.get("ok", 0),
            "captions_rate_limited": stats.captions.get("rate_limited", 0),
            "captions_skipped": stats.captions_skipped,
            "audio_fallbacks": stats.audio,
            "audio_wall_s": round(stats.audio_wall_s, 1),
            "captions_cooldown_active": stats.cooldown_until is not None,
        }
    )
    return result


def _scout_note(ctx: JobContext, result: ScoutRunResult, about: list[str]) -> None:
    """One ``observation`` note per scout run from the batches' ``scan_summary`` (D27)."""
    from pydantic import ValidationError

    from arc.context.kinds import Evidence, NotePayload, NoteTopic

    if not result.summaries:
        return
    urls = list(dict.fromkeys(result.summary_sources))[:20]
    try:
        payload = NotePayload(
            persona="scout",
            topic=NoteTopic.OBSERVATION,
            title=f"Scan summary ({result.docs_scouted} docs)",
            body="\n\n".join(result.summaries)[:4000],
            about=about,
            evidence=[Evidence(ref=u) for u in urls],
        )
    except ValidationError as exc:
        log.warning("pipeline.note_invalid", persona="scout", error=str(exc))
        return
    ctx.write("note", "market", payload)


def scout_persona(ctx: JobContext, llm: ScoutLLM | None = None) -> JobResult:
    """Scout (E4.2): summarise unscouted docs; write each merged Candidate to context.

    *llm* overrides the Hermes backend (``arc propose --fixtures``, tests).
    """
    from arc.context.kinds import CandidatePayload
    from arc.ingest.scout import run_scout

    kwargs: dict[str, Any] = {"now": ctx.now, "run_id": ctx.run_id}
    if llm is not None:
        kwargs["llm"] = llm
    result = run_scout(ctx.conn, ctx.settings, **kwargs)
    written = [
        ctx.write("candidate", cand.ticker, CandidatePayload.model_validate(cand.model_dump())).id
        for cand in result.candidates
    ]
    _scout_note(ctx, result, written)
    from arc.slack.digests import scout_card

    return JobResult(
        summary=(
            f"{result.docs_scouted} docs → {result.accepted} accepted, "
            f"{len(result.candidates)} candidates today"
            + (f", {result.failed_batches} failed batches" if result.failed_batches else "")
        ),
        metrics={
            "new_candidates": result.accepted,
            "candidates": len(result.candidates),
            "docs_scouted": result.docs_scouted,
            "failed_batches": result.failed_batches,
        },
        card=scout_card(
            docs=result.docs_scouted,
            accepted=result.accepted,
            candidates=result.candidates,
            rejected=result.rejected,
            rejected_items=result.rejected_items,
            rationales=result.rationales,
            failed_batches=result.failed_batches,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
        ),
    )


BUILTIN_HANDLERS: Mapping[str, str] = {
    "rss": "arc.routines.handlers:rss_source",
    "edgar": "arc.routines.handlers:edgar_source",
    "earnings": "arc.routines.handlers:earnings_source",
    "youtube": "arc.routines.handlers:youtube_source",
    "scout": "arc.routines.handlers:scout_persona",
    # E5.2 pipeline chain: director → quant → risk → propose (arc/pipeline/steps.py)
    "director": "arc.pipeline.steps:director_step",
    "quant": "arc.pipeline.steps:quant_step",
    "risk": "arc.pipeline.steps:risk_step",
    "propose": "arc.pipeline.steps:propose_step",
    # E5.3 intraday monitor (read-only: positions, Greeks, expiries, daily-loss halt)
    "monitor": "arc.routines.monitor:monitor_step",
    # E6.2 Investor: works an approved proposal through its D24 price band
    "investor": "arc.routines.investor:investor_step",
    # E6.3 Auditor: post-market reconcile (broker vs local), snapshots, tax lots, card
    "auditor": "arc.routines.auditor:auditor_step",
    # E6.4 position manager: review -> exits -> close-to-reallocate (arc/positions/steps.py)
    "positions.evaluate": "arc.positions.steps:evaluate_step",
    "investor.exits": "arc.positions.steps:exits_step",
    "risk.reallocate": "arc.positions.steps:reallocate_step",
}


def import_handler(path: str) -> Handler:
    """Import ``package.module:function``."""
    module_name, _, attr = path.partition(":")
    module = importlib.import_module(module_name)
    fn = getattr(module, attr)
    if not callable(fn):
        msg = f"handler {path!r} is not callable"
        raise TypeError(msg)
    return fn  # type: ignore[no-any-return]


def resolve_handler(
    name: str,
    spec: StepSpec,
    overrides: Mapping[str, Handler] | None = None,
) -> Handler:
    """Find the handler for job/step *name* (see module docstring for order)."""
    overrides = overrides or {}
    if name in overrides:
        return overrides[name]
    if spec.handler:
        return import_handler(spec.handler)
    prefix = name.split(".", 1)[0]
    for key in (name, prefix):
        if key in overrides:
            return overrides[key]
        if key in BUILTIN_HANDLERS:
            return import_handler(BUILTIN_HANDLERS[key])
    return not_implemented
