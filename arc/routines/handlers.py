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
    from arc.models import RawDoc
    from arc.routines.config import JobKind, RoutinesConfig, StepSpec
    from arc.routines.runs import RoutineEvent

log = structlog.get_logger(__name__)


class JobSkippedError(Exception):
    """Raised by a handler to record its run as ``skipped`` (not a failure)."""


@dataclass
class JobResult:
    """What a handler reports back. ``metrics`` feed trigger conditions."""

    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


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
    _settings: ArcSettings | None = None

    @property
    def settings(self) -> ArcSettings:
        if self._settings is None:
            if self.settings_factory is not None:
                self._settings = self.settings_factory()
            else:
                from arc.config import get_settings

                self._settings = get_settings()
        return self._settings

    @property
    def options(self) -> dict[str, Any]:
        return self.spec.options

    def write(
        self,
        kind: str,
        subject: str,
        payload: BaseModel | Mapping[str, object],
        *,
        valid_from: _dt.datetime | None = None,
    ) -> ContextEntry:
        """Append a context entry using this job's TTL/supersede policy."""
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
    """One YouTube channel per job (``channel:`` option), else the configured list."""
    from arc.ingest.youtube import fetch_youtube

    settings = ctx.settings
    channel = ctx.options.get("channel")
    if channel:
        settings = settings.model_copy(update={"ingest_youtube_channels": [youtube_url(channel)]})
    return _source_result(ctx, fetch_youtube(ctx.conn, settings))


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
    for cand in result.candidates:
        ctx.write("candidate", cand.ticker, CandidatePayload.model_validate(cand.model_dump()))
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
