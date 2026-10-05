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
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3
    from collections.abc import Mapping

    from pydantic import BaseModel

    from arc.config import ArcSettings
    from arc.context.ttl import Ttl
    from arc.data.base import MarketDataProvider
    from arc.ingest.llm import ScoutLLM
    from arc.ingest.scout import ScoutRunResult
    from arc.models import RawDoc
    from arc.routines.config import JobKind, RoutinesConfig, StepSpec
    from arc.routines.manifest import ExternalInput
    from arc.routines.runs import RoutineEvent
    from arc.slack.blocks import CardView
    from arc.universe.guard import UniverseGuard

log = structlog.get_logger(__name__)


class JobSkippedError(Exception):
    """Raised by a handler to record its run as ``skipped`` (not a failure).

    ``notice`` (optional) is posted to the day thread like :attr:`JobResult.notice`:
    for a skip a human must act on (E4.1d: no earnings API key), deduped by the handler.
    """

    def __init__(self, *args: object, notice: str = "") -> None:
        super().__init__(*args)
        self.notice = notice


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
    # E5.9 (D33): an OK result that ends the chain here (e.g. the Director found no trade:
    # no Quant/Risk LLM calls). Recorded as ``metrics["stop_chain"]`` for the audit trail.
    stop_chain: bool = False
    # E10.5 (D44): further cards posted to the day thread after this run's own post,
    # one message each (e.g. one stop card per stopped experiment).
    extra_cards: list[CardView] = field(default_factory=list)


@dataclass(frozen=True)
class RunEnv:
    """How this dispatcher process was started (D34: what a spawned Investor inherits).

    ``db_path`` / ``config_path`` / ``lock_dir`` are the CLI's ``--db`` /
    ``--config`` / ``--lock-dir`` (None = the defaults); ``slack`` is False under
    ``--no-slack`` or a dry run.
    """

    db_path: str | None = None
    config_path: str | None = None
    lock_dir: str | None = None
    slack: bool = False


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
    # D34: the process environment a handler needs to hand work to a subprocess.
    run_env: RunEnv = field(default_factory=RunEnv)
    # Why this run started: schedule | manual | manual:<what> | event:<name> | chain:<root>.
    reason: str = "schedule"

    @property
    def is_loop_run(self) -> bool:
        """D31: a scheduled slot of the trading-loop persona (never a manual run)."""
        return self.reason == "schedule" and self.routines.is_loop(self.job)

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
        ttl: Ttl | None = None,
    ) -> ContextEntry:
        """Append a context entry using this job's TTL/supersede policy.

        *ttl* (D47) shortens the policy TTL for this one entry (e.g. a story built
        from 6h-fresh news); it can never lengthen it: the earlier expiry wins.

        Raises :class:`ContractViolationError` (nothing is written) when *kind*
        is not in the job's declared ``writes`` (D27, fail-closed).
        """
        declared = list(self.spec.writes or [])
        if kind not in declared:
            log.error("context.write_rejected", job=self.job, kind=kind, declared=declared)
            msg = f"job {self.job!r} wrote kind {kind!r} not in its declared writes {declared}"
            raise ContractViolationError(msg)
        policy = self.routines.context_policy(kind, self.job)
        eff = policy.ttl
        if ttl is not None:
            from arc.context.categories import earliest_ttl

            at = valid_from or self.now
            eff = earliest_ttl([t for t in (policy.ttl, ttl) if t is not None], at)
        entry = ContextStore(self.conn).write(
            kind=kind,
            subject=subject,
            payload=payload,
            produced_by=self.job,
            ttl=eff,
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
    """RSS feeds; each feed is its own D30 source (``source_key`` on every doc).

    D47: entries older than their feed's category ``max_age`` are never stored.
    """
    from arc.ingest.rss import fetch_rss
    from arc.ingest.sources import FeedSpec, SourceRegistry

    settings = ctx.settings
    feeds = [FeedSpec.parse(f) for f in ctx.options.get("feeds") or []]
    keys: dict[str, str] = {}
    ages: dict[str, Ttl] = {}
    if feeds:
        settings = settings.model_copy(update={"ingest_rss_feeds": [f.url for f in feeds]})
        keys = {f.url: f.key for f in feeds}
        reg = SourceRegistry.from_routines(ctx.routines)
        ages = {f.url: reg.max_age_for(f.key) for f in feeds if f.key in reg.sources}
    docs = fetch_rss(ctx.conn, settings, source_keys=keys, max_ages=ages, now=ctx.now)
    return _source_result(ctx, docs)


def _data_result(ctx: JobContext, name: str, source: str, payload: object, n: int) -> None:
    ctx.record_input(name, source, payload, as_of=ctx.now, count=n)


def vol_term_source(ctx: JobContext) -> JobResult:
    """E4.5: VIX9D / VIX / VIX3M / VVIX closes (Cboe) -> one ``vol_term`` entry."""
    from arc.ingest.options_data import fetch_vol_term

    band = float(ctx.options.get("flat_band", 0.02))
    payload = fetch_vol_term(band=band)
    if payload is None:
        msg = "Cboe VIX history unavailable"
        raise JobSkippedError(msg)
    _data_result(ctx, "vol_term", "cboe", payload.model_dump(mode="json"), 4)
    ctx.write("vol_term", "market", payload)
    return JobResult(
        summary=(
            f"VIX {payload.vix:.2f} · VIX3M/VIX {payload.ratio_3m_1m} ({payload.structure})"
            f" · as of {payload.as_of}"
        ),
        metrics={"vix": payload.vix, "ratio_3m_1m": payload.ratio_3m_1m or 0.0},
    )


def put_call_source(ctx: JobContext) -> JobResult:
    """E4.5: Cboe daily put/call ratios -> one ``put_call`` entry."""
    from arc.ingest.options_data import fetch_put_call

    payload = fetch_put_call(ctx.now.astimezone(ET).date())
    if payload is None:
        msg = "no Cboe put/call ratios in the last week"
        raise JobSkippedError(msg)
    _data_result(ctx, "put_call", "cboe", payload.model_dump(mode="json"), 1)
    ctx.write("put_call", "market", payload)
    return JobResult(
        summary=f"put/call total {payload.total} · equity {payload.equity} · {payload.as_of}",
        metrics={"total": payload.total or 0.0, "equity": payload.equity or 0.0},
    )


def macro_calendar_source(ctx: JobContext) -> JobResult:
    """E4.5/E4.10: FOMC + BLS (CPI/PPI/NFP/JOLTS/ECI) + BEA (GDP/PCE) -> ``macro_calendar``."""
    from arc.ingest.options_data import fetch_macro_calendar

    horizon = int(ctx.options.get("horizon_days", ctx.settings.ingest_macro_horizon_days))
    status: dict[str, str] = {}
    payload, counts = fetch_macro_calendar(
        ctx.now.astimezone(ET).date(),
        horizon,
        contact_ua=ctx.settings.edgar_user_agent,
        status=status,
    )
    if not any(counts.values()):
        msg = "FOMC, BLS and BEA calendars all unavailable"
        raise JobSkippedError(msg)
    _data_result(
        ctx, "macro_calendar", "fed+bls+bea", payload.model_dump(mode="json"), len(payload.events)
    )
    ctx.write("macro_calendar", "market", payload)
    nxt = payload.events[0] if payload.events else None
    return JobResult(
        summary=(
            f"{len(payload.events)} events in {horizon}d"
            + (f" · next {nxt.kind.upper()} {nxt.date}" if nxt else "")
            + "".join(f" · {k} unavailable" for k, v in counts.items() if not v)
        ),
        metrics={
            "events": len(payload.events),
            **{f"{k}_events": v for k, v in counts.items()},
            **{f"{k}_status": v for k, v in status.items()},
        },
    )


def _data_tickers(ctx: JobContext) -> list[str]:
    """Seed universe plus today's candidates (what the chain may trade)."""
    tickers = [str(t).upper() for t in ctx.options.get("tickers") or ctx.settings.universe]
    rows = ctx.conn.execute(
        "SELECT DISTINCT ticker FROM candidates WHERE day = ?",
        (ctx.now.astimezone(ET).date().isoformat(),),
    ).fetchall()
    return list(dict.fromkeys([*tickers, *(r[0] for r in rows)]))


def unusual_options_source(ctx: JobContext, market: MarketDataProvider | None = None) -> JobResult:
    """E4.5: self-computed unusual options activity per ticker (Alpaca chain snapshot)."""
    from arc.ingest.options_data import UoaThresholds, scan_unusual

    s = ctx.settings
    if market is None:  # pragma: no cover - live Alpaca (integration)
        from arc.data.alpaca import AlpacaMarketData

        market = AlpacaMarketData()
    provider: MarketDataProvider = market
    t = UoaThresholds(
        min_volume=s.uoa_min_volume,
        vol_oi_ratio=s.uoa_vol_oi_ratio,
        volume_spike_ratio=s.uoa_volume_spike_ratio,
        min_dte=s.uoa_min_dte,
        min_open_interest=s.uoa_min_open_interest,
        min_hot_share=s.uoa_min_hot_share,
    )
    tickers = _data_tickers(ctx)
    payloads, errors = scan_unusual(
        ctx.conn,
        provider,
        tickers,
        ctx.now.astimezone(ET).date(),
        t,
        max_dte=s.uoa_max_dte,
        now=ctx.now.isoformat(),
    )
    _data_result(
        ctx, "option_chains", "alpaca", [p.model_dump(mode="json") for p in payloads], len(payloads)
    )
    flagged = [p for p in payloads if p.flags]
    for p in payloads:
        ctx.write("unusual_options", p.ticker, p)
    return JobResult(
        summary=(
            f"{len(payloads)} tickers · {len(flagged)} unusual"
            + (f" ({', '.join(p.ticker for p in flagged[:8])})" if flagged else "")
            + (f" · {len(errors)} chain errors" if errors else "")
        ),
        metrics={"tickers": len(payloads), "unusual": len(flagged), "errors": len(errors)},
    )


def ex_dividend_source(ctx: JobContext) -> JobResult:
    """E4.5: next cash-dividend ex-date per ticker (Alpaca corporate actions)."""
    from arc.ingest.options_data import fetch_ex_dividends

    horizon = int(ctx.options.get("horizon_days", ctx.settings.ex_dividend_horizon_days))
    tickers = _data_tickers(ctx)
    found = fetch_ex_dividends(tickers, ctx.now.astimezone(ET).date(), horizon)
    _data_result(
        ctx,
        "corporate_actions",
        "alpaca",
        {k: v.model_dump(mode="json") for k, v in found.items()},
        len(found),
    )
    for ticker, payload in sorted(found.items()):
        ctx.write("ex_dividend", ticker, payload)
    return JobResult(
        summary=f"{len(found)} ex-dividend dates within {horizon}d of {len(tickers)} tickers",
        metrics={"ex_dividends": len(found), "tickers": len(tickers)},
    )


def edgar_source(ctx: JobContext) -> JobResult:
    """SEC filings (D47: published = accepted time; older than ``max_age`` never stored)."""
    from arc.ingest.edgar import fetch_edgar
    from arc.ingest.sources import SourceRegistry

    settings = ctx.settings
    tickers = ctx.options.get("tickers")
    if tickers:
        settings = settings.model_copy(update={"universe": list(tickers)})
    reg = SourceRegistry.from_routines(ctx.routines)
    max_age = reg.max_age_for(ctx.job) if ctx.job in reg.sources else None
    return _source_result(ctx, fetch_edgar(ctx.conn, settings, now=ctx.now, max_age=max_age))


EARNINGS_NO_KEY_DAY = "earnings:no_api_key_notice"


def earnings_source(ctx: JobContext) -> JobResult:
    """E4.1d: no key -> ``skipped`` (``no_api_key``, one notice per day); a fetch,
    HTTP, JSON, truncation or rate-limit error propagates -> ``failed`` + alert.

    Fetch knobs (``row_cap``, ``chunk_days``, ``rate_limit_per_min``, ...) are the
    job's options in ``config/routines.yaml``.
    """
    from arc.ingest.earnings import EarningsFetchConfig, EarningsNoKeyError, fetch_earnings
    from arc.routines.runs import RoutineStateRepo

    cfg = EarningsFetchConfig.from_options(ctx.options)
    try:
        docs = fetch_earnings(ctx.conn, ctx.settings, cfg=cfg, today=ctx.now.date())
    except EarningsNoKeyError as exc:
        state = RoutineStateRepo(ctx.conn)
        day = ctx.now.astimezone(ET).date().isoformat()
        notice = ""
        if state.get(EARNINGS_NO_KEY_DAY) != day:
            state.set(EARNINGS_NO_KEY_DAY, day, now=ctx.now)
            notice = (
                "earnings calendar skipped: no_api_key (set ARC_FINNHUB_API_KEY in "
                "~/.hermes/.env); next_earnings is empty, so short premium on stocks "
                "fails closed"
            )
        raise JobSkippedError(str(exc), notice=notice) from exc
    return _source_result(ctx, docs)


# ---------------------------------------------------------------------------
# E4.8 (D46): Finnhub per-ticker context jobs (typed kinds, never raw docs)
# ---------------------------------------------------------------------------

FINNHUB_NO_KEY_DAY = "finnhub:no_api_key_notice"
FINNHUB_MAX_FAILED_SHARE = 0.5  # more than half the tickers failed -> the run fails
FINNHUB_EARNINGS_FULL_KEY = "finnhub:earnings_history:last_full"


def _finnhub_run(ctx: JobContext, kind: str, *, recent_only: bool = False) -> JobResult:
    """Shared body of the four Finnhub context jobs (E4.1d outcome rules).

    No key -> ``skipped`` (``no_api_key``, one notice per day). 403 -> ``failed``
    (``forbidden``), 429 after one retry -> ``failed`` (``rate_limited``). All tickers
    failed, or more than half -> ``failed``; fewer -> ``ok`` with ``failed_tickers``.
    Job options: ``tickers`` (replaces the seed list), ``max_tickers``.
    """
    import time

    from arc.ingest.finnhub import DbRateLimiter, FinnhubClient, FinnhubError, FinnhubNoKey
    from arc.ingest.finnhub_context import (
        InsiderRules,
        fetch_per_ticker,
        recent_reporters,
        ticker_scope,
    )
    from arc.pipeline.market import ETF_UNDERLYINGS
    from arc.routines.runs import RoutineStateRepo
    from arc.universe.ingest import IngestUniverse

    started = time.monotonic()
    s = ctx.settings
    try:
        client = FinnhubClient(
            s.finnhub_api_key,
            limiter=DbRateLimiter(ctx.conn, calls_per_minute=s.finnhub_calls_per_minute),
        )
    except FinnhubNoKey as exc:
        state = RoutineStateRepo(ctx.conn)
        day = ctx.now.astimezone(ET).date().isoformat()
        notice = ""
        if state.get(FINNHUB_NO_KEY_DAY) != day:
            state.set(FINNHUB_NO_KEY_DAY, day, now=ctx.now)
            notice = (
                "Finnhub context skipped: no_api_key (set ARC_FINNHUB_API_KEY in "
                "~/.hermes/.env); earnings history, insider, analyst and fundamentals "
                "context is not refreshed"
            )
        raise JobSkippedError(str(exc), notice=notice) from exc

    today = ctx.now.astimezone(ET).date()
    seed = ctx.options.get("tickers") or s.universe
    universe = IngestUniverse.from_settings(s, now=ctx.now)
    scope = ticker_scope(
        ctx.conn,
        seed=seed,
        now=ctx.now,
        max_tickers=int(ctx.options.get("max_tickers", s.finnhub_max_tickers)),
        master=universe.master,
        etfs=ETF_UNDERLYINGS,
    )
    tickers = scope.tickers
    if recent_only:
        lo, hi = (int(x) for x in ctx.options.get("recent_report_days", [1, 3]))
        tickers = recent_reporters(ctx.conn, tickers, today, lo, hi)
    rules = InsiderRules(
        window_days=s.finnhub_insider_window_days,
        cluster_buyers=s.finnhub_cluster_buyers,
        cluster_days=s.finnhub_cluster_days,
    )
    try:
        run = fetch_per_ticker(
            client,
            kind,  # type: ignore[arg-type]
            tickers,
            today=today,
            write=lambda t, p: ctx.write(kind, t, p),
            rules=rules,
        )
    finally:
        # D27: endpoints, call count and as_of in the run manifest (never the key).
        ctx.record_input(
            "finnhub:" + ",".join(sorted(client.endpoints)),
            "finnhub",
            {"endpoints": client.endpoints, "tickers": tickers},
            as_of=ctx.now,
            count=client.calls,
        )
    duration = round(time.monotonic() - started, 1)
    log.info(
        "finnhub.done",
        job=ctx.job,
        kind=kind,
        calls=client.calls,
        tickers=len(tickers),
        written=run.written,
        empty=len(run.empty),
        failed=len(run.failed),
        dropped=len(scope.dropped),
        etfs_skipped=len(scope.etfs_skipped),
        duration_s=duration,
    )
    metrics: dict[str, Any] = {
        "calls": client.calls,
        "tickers": len(tickers),
        "written": run.written,
        "empty_tickers": run.empty,
        "failed_tickers": sorted(run.failed),
        "duration_s": duration,
        "endpoints": dict(client.endpoints),
        "as_of": today.isoformat(),
        "scope": {**scope.sources, "dropped": len(scope.dropped), "etfs": len(scope.etfs_skipped)},
        "mode": "recent_reporters" if recent_only else "full",
    }
    if run.failed and (
        len(run.failed) == len(tickers) or len(run.failed) / len(tickers) > FINNHUB_MAX_FAILED_SHARE
    ):
        sample = "; ".join(f"{t}: {e}" for t, e in list(run.failed.items())[:3])
        msg = f"{len(run.failed)}/{len(tickers)} tickers failed ({sample})"
        raise FinnhubError(msg)
    summary = (
        f"{run.written} {kind} written · {len(tickers)} tickers · {client.calls} calls"
        + (
            f" · {len(run.failed)} failed ({', '.join(sorted(run.failed)[:5])})"
            if run.failed
            else ""
        )
        + (f" · {len(scope.dropped)} over cap" if scope.dropped else "")
    )
    return JobResult(summary=summary, metrics=metrics)


def finnhub_insider_source(ctx: JobContext) -> JobResult:
    """E4.8: open-market insider buys/sells per ticker -> ``insider_activity``."""
    return _finnhub_run(ctx, "insider_activity")


def finnhub_recs_source(ctx: JobContext) -> JobResult:
    """E4.8: monthly analyst recommendation trend per ticker -> ``analyst_recs``."""
    return _finnhub_run(ctx, "analyst_recs")


def finnhub_fundamentals_source(ctx: JobContext) -> JobResult:
    """E4.8: trimmed basic financials per ticker -> ``fundamentals``."""
    return _finnhub_run(ctx, "fundamentals")


def finnhub_earnings_history_source(ctx: JobContext) -> JobResult:
    """E4.8: EPS surprises per ticker -> ``earnings_history``.

    The whole scope once a week: on a ``full_days`` weekday (default Monday), or on
    any run when the last full run is ``full_every_days`` (default 7) or more days
    old (a Monday holiday still gets its weekly refresh on Tuesday). Other runs
    fetch only the tickers whose earnings date was ``recent_report_days`` (default
    1-3) days ago, so a fresh report lands the morning after.
    """
    from arc.routines.config import Weekday
    from arc.routines.runs import RoutineStateRepo

    today = ctx.now.astimezone(ET).date()
    full_days = {str(d).lower()[:3] for d in ctx.options.get("full_days", ["mon"])}
    full = any(Weekday(d).weekday_index == today.weekday() for d in full_days)
    state = RoutineStateRepo(ctx.conn)
    last = state.get(FINNHUB_EARNINGS_FULL_KEY)
    every = int(ctx.options.get("full_every_days", 7))
    if last is None or (today - _date_of(last)).days >= every:
        full = True
    result = _finnhub_run(ctx, "earnings_history", recent_only=not full)
    if full:
        state.set(FINNHUB_EARNINGS_FULL_KEY, today.isoformat(), now=ctx.now)
    return result


def _date_of(text: str) -> _dt.date:
    import datetime as dt

    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return dt.date.min


def symbols_source(ctx: JobContext) -> JobResult:
    """Weekly symbol-master refresh (E5.7 / D28): SEC tickers ∪ Alpaca optionable.

    The only scheduled writer of the cache that ingest and the Scout read (they
    never fetch mid-run). Writes no context; the symbol list's digest is recorded.
    """
    from arc.universe import load_universe_config, refresh_symbol_master

    cfg = load_universe_config(ctx.settings.universe_config_file)
    master = refresh_symbol_master(
        cfg.symbol_master, user_agent=ctx.settings.edgar_user_agent, now=ctx.now
    )
    ctx.record_input(
        "symbol_master",
        "sec+alpaca",
        sorted(master.symbols),
        as_of=master.fetched_at,
        count=len(master.symbols),
    )
    optionable = sum(1 for s in master.symbols.values() if s.options)
    return JobResult(
        summary=f"{len(master.symbols)} symbols ({optionable} optionable)",
        metrics={"symbols": len(master.symbols), "optionable": optionable, **master.sources},
    )


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


_UNSET: Any = object()


def _market_price_lookup() -> Callable[[str], float | None] | None:
    """Reference underlying price for level sanity checks (market data, never the broker)."""
    try:
        from arc.data.alpaca import AlpacaMarketData
        from arc.data.base import reference_price
        from arc.utils.calendar import now_et

        md = AlpacaMarketData()
    except Exception as exc:  # noqa: BLE001 - no keys / no network -> unverified levels
        log.warning("brief.price_lookup_unavailable", error=str(exc))
        return None

    def lookup(symbol: str) -> float | None:
        return reference_price(md, symbol, today=now_et().date())

    return lookup


def youtube_briefs(
    ctx: JobContext,
    llm: ScoutLLM | None = None,
    *,
    session: Any = None,
    list_videos: Callable[[str, int], list[dict[str, Any]]] | None = None,
    fetch_info: Callable[[str], Mapping[str, Any]] | None = None,
    price_lookup: Any = _UNSET,
) -> JobResult:
    """E4.6 (D45): the daily 05:00 ET YouTube brief run, all channels in one job.

    Per channel: newest qualifying video in ``lookback`` -> transcript -> brief ->
    ``channel_brief`` context entry (subject ``youtube.<slug>``, the job's
    ``context`` TTL, supersede latest) plus a ``raw_doc_ref``. The keyword
    arguments replace the network pieces in tests.
    """
    from pathlib import Path

    from arc.context.kinds import ChannelBriefPayload, RawDocRefPayload
    from arc.ingest.channels import CHANNELS_DIR, ChannelRegistry, default_registry
    from arc.ingest.channels.daily import DailyBriefConfig, run_daily_briefs
    from arc.ingest.llm import HermesScoutLLM
    from arc.ingest.youtube import TranscriptSession, _get_video_info, list_channel_videos
    from arc.universe.ingest import IngestUniverse

    settings = ctx.settings
    cfg = DailyBriefConfig.from_options(ctx.options)
    policy = ctx.routines.context_policy("channel_brief", ctx.job)
    ttl = policy.ttl.duration if policy.ttl is not None and policy.ttl.duration else None
    if ttl is None:
        msg = f"{ctx.job}: context.ttl must be a duration (e.g. 24h)"
        raise ValueError(msg)
    now = ctx.now.astimezone(ET)
    session = session or TranscriptSession.start(ctx.conn, settings, now=now)

    def write_brief(cr: Any) -> None:
        ctx.write(
            "channel_brief",
            cr.channel.source_key,
            ChannelBriefPayload.model_validate(cr.brief.model_dump()),
        )

    run = run_daily_briefs(
        ctx.conn,
        settings,
        cfg,
        llm=llm or HermesScoutLLM.from_settings(settings),
        session=session,
        now=now,
        ttl=ttl,
        universe=IngestUniverse.from_settings(settings, now=now),
        registry=(
            ChannelRegistry.load(CHANNELS_DIR, Path(str(ctx.options["profiles_dir"])))
            if ctx.options.get("profiles_dir")
            else default_registry()
        ),
        list_videos=list_videos or (lambda url, n: list_channel_videos(url, max_videos=n)),
        fetch_info=fetch_info or _get_video_info,
        write_brief=write_brief,
        price_lookup=_market_price_lookup() if price_lookup is _UNSET else price_lookup,
    )
    for cr in run.channels:  # raw_doc_ref for every transcript stored this run
        if cr.raw_doc_id and cr.pick.published_at is not None:
            ctx.write(
                "raw_doc_ref",
                cr.raw_doc_id,
                RawDocRefPayload(
                    doc_id=cr.raw_doc_id,
                    source="youtube",
                    url=f"https://www.youtube.com/watch?v={cr.pick.video_id}",
                    published_at=cr.pick.published_at.isoformat(),
                ),
            )
    stats = session.finish()
    ctx.record_input(
        "youtube_videos",
        "youtube",
        {c.channel.slug: c.pick.video_id for c in run.channels},
        as_of=now,
        count=sum(c.pick.scanned for c in run.channels),
    )
    summary = f"{run.summary()} · {stats.summary()}"
    errors = run.errors
    notice = ""
    if errors:
        notice = "YouTube briefs: " + "; ".join(
            f"{c.channel.display} failed ({c.error})" for c in errors
        )
    tokens_in = sum(c.input_tokens or 0 for c in run.channels)
    tokens_out = sum(c.output_tokens or 0 for c in run.channels)
    return JobResult(
        summary=summary,
        notice=notice,
        metrics={
            "briefs": run.present,
            "channels_total": len(run.channels),
            "channels_failed": len(errors),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "captions_ok": stats.captions.get("ok", 0),
            "captions_rate_limited": stats.captions.get("rate_limited", 0),
            "captions_skipped": stats.captions_skipped,
            "audio_fallbacks": stats.audio,
            "audio_wall_s": round(stats.audio_wall_s, 1),
            "captions_cooldown_active": stats.cooldown_until is not None,
            "channels": {c.channel.slug: c.manifest() for c in run.channels},
        },
    )


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


def _journal_universe_rejects(ctx: JobContext, result: ScoutRunResult) -> int:
    """E7.4: one ``candidate``-stage decision per universe reject (D28). Returns the count."""
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore

    codes = {
        "not_in_universe": ReasonCode.UNIVERSE_NOT_IN_UNIVERSE,
        "unknown_symbol": ReasonCode.UNIVERSE_UNKNOWN_SYMBOL,
        "illiquid": ReasonCode.UNIVERSE_ILLIQUID,
        "over_new_ticker_cap": ReasonCode.UNIVERSE_NEW_TICKER_CAP,
    }
    store = JournalStore(ctx.conn)
    n = 0
    for key, code in codes.items():
        for ticker in dict.fromkeys(result.rejected_items.get(key, [])):
            store.record(
                persona=JournalPersona.SCOUT,
                stage=Stage.CANDIDATE,
                subject=ticker,
                choice=Choice.REJECTED,
                reason_code=code,
                reason_text=result.reject_details.get(ticker, ""),
                at=ctx.now,
                chain_run_id=ctx.chain_run_id,
                run_id=ctx.run_id,
            )
            n += 1
    return n


def scout_persona(
    ctx: JobContext, llm: ScoutLLM | None = None, guard: UniverseGuard | None = None
) -> JobResult:
    """Scout (E4.2): summarise unscouted docs; write each merged Candidate to context.

    *llm* overrides the Hermes backend and *guard* the D28 universe policy
    (``arc propose --fixtures``, tests).
    """
    from arc.context.kinds import CandidatePayload
    from arc.ingest.scout import run_scout

    kwargs: dict[str, Any] = {"now": ctx.now, "run_id": ctx.run_id, "routines": ctx.routines}
    if llm is not None:
        kwargs["llm"] = llm
    if guard is not None:
        kwargs["guard"] = guard
    write_stories = "story" in (ctx.spec.writes or [])  # D30 stage-1 digests
    result = run_scout(ctx.conn, ctx.settings, **kwargs)
    if write_stories:  # D47: each story expires at min(policy, freshest source max_age + 2h)
        for p in result.stories:
            ctx.write("story", p.story_id, p, ttl=result.story_ttls.get(p.story_id))
    _journal_universe_rejects(ctx, result)
    written = [
        ctx.write(
            "candidate",
            cand.ticker,
            CandidatePayload.model_validate(cand.model_dump()),
            ttl=result.candidate_ttls.get(cand.ticker),
        ).id
        for cand in result.candidates
    ]
    _scout_note(ctx, result, written)
    from arc.slack.digests import scout_card

    return JobResult(
        summary=(
            f"{result.docs_scouted} docs ({len(result.stories)} stories) → "
            f"{result.accepted} accepted, {len(result.candidates)} candidates today"
            + (f", {result.over_budget} over budget" if result.over_budget else "")
            + (f", {result.failed_batches} failed batches" if result.failed_batches else "")
        ),
        metrics={
            "new_candidates": result.accepted,
            "candidates": len(result.candidates),
            "docs_scouted": result.docs_scouted,
            "stories": len(result.stories),
            "over_budget": result.over_budget,
            "skipped_budget": result.skipped_budget,
            "skipped_stale": result.skipped_stale,
            "digest_batches": result.digest_batches,
            "failed_digest_batches": result.failed_digest_batches,
            "failed_batches": result.failed_batches,
            "new_tickers": len(result.new_tickers),
            **{f"source_{label}": read for label, read, _ in result.source_mix},
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
            reject_details=result.reject_details,
            new_tickers=result.new_tickers,
            source_mix=result.source_mix,
            stories=len(result.stories),
            category_mix=result.category_mix,
        ),
    )


BUILTIN_HANDLERS: Mapping[str, str] = {
    "rss": "arc.routines.handlers:rss_source",
    "edgar": "arc.routines.handlers:edgar_source",
    "earnings": "arc.routines.handlers:earnings_source",
    "symbols": "arc.routines.handlers:symbols_source",  # E5.7 weekly symbol master
    "youtube": "arc.routines.handlers:youtube_source",
    "youtube.briefs": "arc.routines.handlers:youtube_briefs",  # E4.6 daily briefs (D45)
    # E4.5 / D30 options-trading data (no LLM; typed context kinds)
    "vol_term": "arc.routines.handlers:vol_term_source",
    "put_call": "arc.routines.handlers:put_call_source",
    "macro_calendar": "arc.routines.handlers:macro_calendar_source",
    "unusual_options": "arc.routines.handlers:unusual_options_source",
    "ex_dividend": "arc.routines.handlers:ex_dividend_source",
    # E4.8 (D46): Finnhub per-ticker context (typed kinds, shared 55/min budget)
    "finnhub.insider": "arc.routines.handlers:finnhub_insider_source",
    "finnhub.recs": "arc.routines.handlers:finnhub_recs_source",
    "finnhub.fundamentals": "arc.routines.handlers:finnhub_fundamentals_source",
    "finnhub.earnings_history": "arc.routines.handlers:finnhub_earnings_history_source",
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
    # D34 in-chain Execute: publish + auto-approve this chain's proposals, hand each
    # to an Investor subprocess (own lock, not the LLM lock); a no-op when auto is off
    "execute": "arc.routines.investor:execute_step",
    # E6.3 Auditor: post-market reconcile (broker vs local), snapshots, tax lots, card
    "auditor": "arc.routines.auditor:auditor_step",
    # E6.4 position manager: review -> exits -> close-to-reallocate (arc/positions/steps.py)
    "positions.evaluate": "arc.positions.steps:evaluate_step",
    "investor.exits": "arc.positions.steps:exits_step",
    "risk.reallocate": "arc.positions.steps:reallocate_step",
    # E7.3 weekly paper scorecard (deterministic, from the audit store)
    "scorecard": "arc.routines.scorecard:scorecard_step",
    # E10.3 (D44): daily experiment evaluation after the EOD reconcile
    "experiments.evaluate": "arc.routines.experiments:experiments_evaluate_step",
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
