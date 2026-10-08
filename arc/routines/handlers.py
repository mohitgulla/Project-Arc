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
   (persona handlers owned by later cards).
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
    from arc.context.kinds import ScoutReadPayload
    from arc.context.ttl import Ttl
    from arc.data.base import MarketDataProvider
    from arc.ingest.llm import PersonaLLM
    from arc.ingest.scalp import ScalpRunResult
    from arc.models import RawDoc
    from arc.routines.config import JobKind, RoutinesConfig, StepSpec
    from arc.routines.manifest import ExternalInput
    from arc.routines.runs import RoutineEvent
    from arc.slack.blocks import CardView
    from arc.universe.guard import UniverseGuard
    from arc.universe.tiers import ActiveUniverse

log = structlog.get_logger(__name__)


class JobSkippedError(Exception):
    """Raised by a handler to record its run as ``skipped`` (not a failure).

    ``notice`` (optional) is posted to the day thread like :attr:`JobResult.notice`:
    for a skip a human must act on (E4.1d: no earnings API key), deduped by the handler.
    """

    def __init__(self, *args: object, notice: str = "", continue_chain: bool = False) -> None:
        super().__init__(*args)
        self.notice = notice
        # E13.9: an optional chain step (quant.revise) skipping itself does not stop
        # the chain; the next step runs as if it had succeeded.
        self.continue_chain = continue_chain


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
    # E5.9 (D33): an OK result that ends the chain here (e.g. Research found no trade:
    # no Quant/Risk LLM calls). Recorded as ``metrics["stop_chain"]`` for the audit trail.
    stop_chain: bool = False
    # E10.5 (D44): further cards posted to the day thread after this run's own post,
    # one message each (e.g. one stop card per stopped experiment).
    extra_cards: list[CardView] = field(default_factory=list)


@dataclass(frozen=True)
class RunEnv:
    """How this dispatcher process was started (D34: what a spawned Broker inherits).

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
        digest: str | None = None,
    ) -> ExternalInput:
        """Record market/broker/DB data this run used (D27 run manifest).

        Only the sha256 of *payload*'s canonical JSON is kept, never the data.
        *digest* (E12.2) records a digest the caller computed instead, e.g. the
        sha256 of a fetched page's raw bytes.
        """
        from arc.routines.manifest import ExternalInput
        from arc.routines.manifest import digest as _digest

        item = ExternalInput(
            name=name,
            source=source,
            as_of=as_of,
            digest=digest or _digest(payload),
            count=count,
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


def registry_label(ctx: JobContext, key: str) -> str:
    """Display label of registry source *key* (D55: retired keys keep their label)."""
    from arc.ingest.sources import SourceRegistry

    return SourceRegistry.from_routines(ctx.routines).spec_for(key).display


def rss_source(ctx: JobContext) -> JobResult:
    """RSS feeds; each feed is its own D30 source (``source_key`` on every doc).

    D47: entries older than their feed's category ``max_age`` are never stored.
    D55: entries a feed's ``title_exclude`` / ``title_include`` filters out are stored
    closed ``filtered`` (no ``raw_doc_ref``); per-feed ``new`` / ``filtered`` counts
    land in the run's metrics (``new_<feed>`` / ``filtered_<feed>``).
    """
    from arc.ingest.rss import fetch_rss_feeds
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
    fetched = fetch_rss_feeds(
        ctx.conn,
        settings,
        source_keys=keys,
        max_ages=ages,
        feed_specs={f.url: f for f in feeds},
        now=ctx.now,
    )
    res = _source_result(ctx, fetched.docs)
    n_filtered = sum(fetched.filtered.values())
    per_feed = [f"{k} {fetched.new[k]}" for k in keys.values() if fetched.new[k]]
    summary = res.summary
    if per_feed:
        summary += f" ({', '.join(per_feed)})"
    if n_filtered:
        summary += f", {n_filtered} filtered"
    metrics: dict[str, Any] = {**res.metrics, "filtered": n_filtered}
    metrics.update({f"new_{k}": n for k, n in fetched.new.items()})
    metrics.update({f"filtered_{k}": n for k, n in fetched.filtered.items()})
    return JobResult(summary=summary, metrics=metrics)


def _data_result(ctx: JobContext, name: str, source: str, payload: object, n: int) -> None:
    ctx.record_input(name, source, payload, as_of=ctx.now, count=n)


def _vol_term_session_check(ctx: JobContext, as_of: str) -> None:
    """E14.2 (D60): with a per-session ``catch_up`` (``vol_term:{day}``), a slot stores
    only the session it reads (:func:`arc.utils.calendar.completed_session`). Cboe's
    CSV still ending on an older close = not published yet: a skip on the evening slot
    (the session is today), a failure on the morning catch-up (it should be there)."""
    from arc.utils.calendar import completed_session

    cu = getattr(ctx.spec, "catch_up", None)  # chain steps (StepSpec) carry none
    if cu is None or not cu.per_session:
        return
    slot = ctx.scheduled_for.astimezone(ET)
    day = completed_session(slot)
    if as_of >= day.isoformat():
        return
    if day == slot.date():
        msg = f"not published yet: Cboe VIX history ends {as_of}, want {day.isoformat()}"
        raise JobSkippedError(msg)
    msg = f"catch-up: Cboe VIX history still ends {as_of}, want {day.isoformat()}"
    raise RuntimeError(msg)


def vol_term_source(ctx: JobContext) -> JobResult:
    """E4.5: VIX9D / VIX / VIX3M / VVIX closes (Cboe) -> one ``vol_term`` entry."""
    from arc.ingest.options_data import fetch_vol_term

    band = float(ctx.options.get("flat_band", 0.02))
    payload = fetch_vol_term(band=band)
    if payload is None:
        msg = "Cboe VIX history unavailable"
        raise JobSkippedError(msg)
    _vol_term_session_check(ctx, payload.as_of)
    _data_result(ctx, "vol_term", "cboe", payload.model_dump(mode="json"), 4)
    ctx.write("vol_term", "market", payload)
    return JobResult(
        summary=(
            f"VIX {payload.vix:.2f} · VIX3M/VIX {payload.ratio_3m_1m} ({payload.structure})"
            f" · as of {payload.as_of}"
        ),
        metrics={"vix": payload.vix, "ratio_3m_1m": payload.ratio_3m_1m or 0.0},
    )


def _probe_sleep(seconds: float) -> None:  # pragma: no cover - patched in tests
    import time

    time.sleep(seconds)


def _options_slow_fetch(
    ctx: JobContext, fetch: Callable[[_dt.date], BaseModel]
) -> tuple[BaseModel, int]:
    """E13.5: fetch the session a slot reads -> ``(payload, probe wait in seconds)``.

    A not-yet-published session is a skip on the evening slot (the session is today)
    and a failure on the morning catch-up (the session is an earlier day: Cboe
    should have published hours ago). ``options_slow.publish_probe_minutes`` > 0
    (measurement only) re-probes an unpublished evening session once a minute for
    that long before skipping.
    """
    from arc.ingest.cboe_daily import NotPublishedError, session_date

    slot = ctx.scheduled_for.astimezone(ET)
    day = session_date(slot)
    evening = day == slot.date()
    probe = ctx.routines.options_slow.publish_probe_minutes if evening else 0
    waited = 0
    while True:
        try:
            return fetch(day), waited * 60
        except NotPublishedError as exc:
            if waited < probe:
                _probe_sleep(60)
                waited += 1
                continue
            if evening:
                msg = f"not published yet: {exc}"
                raise JobSkippedError(msg) from exc
            msg = f"catch-up: Cboe still has not published {day.isoformat()}: {exc}"
            raise RuntimeError(msg) from exc


def options_daily_source(ctx: JobContext) -> JobResult:
    """E13.5 (D56): Cboe daily options statistics -> one ``options_daily`` entry."""
    from arc.context.kinds import OptionsDailyPayload
    from arc.ingest.cboe_daily import fetch_daily_options

    payload, waited = _options_slow_fetch(ctx, lambda d: fetch_daily_options(d, now=ctx.clock()))
    assert isinstance(payload, OptionsDailyPayload)
    _data_result(ctx, "options_daily", "cboe", payload.model_dump(mode="json"), len(payload.ratios))
    ctx.write("options_daily", "market", payload)
    ratio = {r.segment: r.ratio for r in payload.ratios}
    parts = [f"{seg} {ratio[seg]:.2f}" for seg in ("total", "equity", "spx") if seg in ratio]
    metrics: dict[str, Any] = {f"pc_{k}": v for k, v in ratio.items()}
    metrics["probe_wait_s"] = waited
    return JobResult(
        summary=f"P/C {' · '.join(parts)} · {len(payload.open_interest)} OI rows · {payload.as_of}",
        metrics=metrics,
    )


def vix_futures_source(ctx: JobContext) -> JobResult:
    """E13.5 (D56): CFE VX futures settlements -> one ``vx_curve`` entry."""
    from arc.context.kinds import VxCurvePayload
    from arc.ingest.cboe_daily import fetch_vx_settlements

    band = ctx.routines.options_slow.vx_flat_band
    payload, waited = _options_slow_fetch(
        ctx, lambda d: fetch_vx_settlements(d, now=ctx.clock(), flat_band=band)
    )
    assert isinstance(payload, VxCurvePayload)
    _data_result(ctx, "vx_curve", "cboe_cfe", payload.model_dump(mode="json"), len(payload.points))
    ctx.write("vx_curve", "market", payload)
    return JobResult(
        summary=(
            f"VX {payload.front:.2f} / {payload.second:.2f} … {payload.back:.2f} · "
            f"{payload.slope_1_2_pct:+.2f}% ({payload.shape}) · {payload.as_of}"
        ),
        metrics={
            "front": payload.front,
            "slope_1_2_pct": payload.slope_1_2_pct,
            "probe_wait_s": waited,
        },
    )


def _options_fast_tickers(ctx: JobContext, cap: int) -> list[str]:
    """E13.6: active list (D51/D56) ∪ open underlyings, active first, capped at *cap*.

    ``tickers:`` option ``active_list`` (default) or an explicit list (tests, probes).
    """
    from arc.universe.tiers import active_tickers, open_underlyings

    opt = ctx.options.get("tickers", "active_list")
    base = (
        active_tickers(ctx.conn, ctx.settings, ctx.now)
        if opt in (None, "active_list")
        else [str(t) for t in opt]
    )
    names = [t.strip().upper() for t in [*base, *open_underlyings(ctx.conn)] if t.strip()]
    return list(dict.fromkeys(names))[:cap]


def _cboe_get(max_bytes: int | None = None) -> Callable[[str], bytes]:  # pragma: no cover - live
    from arc.ingest.options_data import http_get

    def get(url: str) -> bytes:
        return http_get(
            url, "Mozilla/5.0 (Project Arc)", timeout=20.0, retries=1, max_bytes=max_bytes
        )

    return get


def options_fast_source(
    ctx: JobContext,
    get: Callable[[str], bytes] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> JobResult:
    """E13.6 (D56): the 30-min options_fast tape, three isolated parts.

    1. ``index_vols`` (subject market): Cboe delayed VIX / VIX9D / VXN / VIX1D / VIX3M / VVIX.
    2. ``chain_snapshot`` (subject = ticker): one delayed chain per ticker in scope
       (active list ∪ open underlyings, ``max_tickers``), serial with ``pace_s`` pacing.
    3. ``exchange_volume`` (subject market): Cboe ``symbol_data`` CSV per market,
       aggregated per underlying (active names + top 10 others).

    A part that fails is listed in ``metrics.failed_parts``; the run is ``ok`` when at
    least one part wrote, and fails (all three failed) otherwise.
    """
    import time

    from arc.context.kinds import ExchangeVolumePayload
    from arc.ingest import cboe_fast as cf

    t0 = time.perf_counter()
    cfg = ctx.routines.options_fast
    csv_get = get or _cboe_get(cfg.max_csv_bytes)  # size guard while downloading
    get = get or _cboe_get()
    sleep = sleep or time.sleep
    now = ctx.now.astimezone(ET)
    fetched_at = now.replace(microsecond=0).isoformat()
    max_tickers = int(ctx.options.get("max_tickers", 50))
    strikes = int(ctx.options.get("strikes", 3))
    pace = float(ctx.options.get("pace_s", 0.2))
    markets = [str(m) for m in ctx.options.get("symbol_data_markets") or ["opt"]]
    tickers = _options_fast_tickers(ctx, max_tickers)
    failed: dict[str, str] = {}
    metrics: dict[str, Any] = {"tickers_requested": len(tickers)}
    parts: list[str] = []

    # -- 1. index vols ---------------------------------------------------------
    vols = None
    errors: dict[str, str] = {}
    try:
        vols = cf.fetch_index_vols(
            get,
            now,
            vix_gt_25=cfg.vix_flags.vix_gt_25,
            vix_gt_35=cfg.vix_flags.vix_gt_35,
            errors=errors,
        )
    except Exception as exc:  # noqa: BLE001 - per-part isolation (E13.6)
        failed["index_vols"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    if vols is not None:
        ctx.record_input(
            "index_vols",
            "cboe_delayed",
            vols.model_dump(mode="json"),
            as_of=ctx.now,
            count=len(vols.quotes),
        )
        ctx.write("index_vols", "market", vols)
        parts.append(
            f"VIX {vols.value('VIX'):.2f}" + (f" ({', '.join(vols.flags)})" if vols.flags else "")
        )
        metrics["vix"] = vols.value("VIX")
    if errors:
        metrics["index_vol_errors"] = errors

    # -- 2. chain snapshots ----------------------------------------------------
    dte_window = ctx.settings.entry_dte_window
    skipped: dict[str, str] = {}
    written: list[str] = []
    snaps_digest: dict[str, Any] = {}
    for i, ticker in enumerate(tickers):
        if i:
            sleep(pace)
        try:
            chain = cf.parse_delayed_chain(get(cf.CBOE_CHAIN_URL.format(ticker=ticker)))
            snap = cf.snapshot_ticker(
                chain,
                ticker=ticker,
                today=now.date(),
                dte_window=dte_window,
                fetched_at=fetched_at,
                strikes=strikes,
            )
        except cf.SnapshotSkipError as exc:
            skipped[ticker] = exc.reason
            continue
        except Exception as exc:  # noqa: BLE001 - one ticker never fails the part
            skipped[ticker] = f"fetch_error:{type(exc).__name__}"
            continue
        ctx.write("chain_snapshot", ticker, snap)
        written.append(ticker)
        snaps_digest[ticker] = [snap.call_volume_td, snap.put_volume_td, snap.expiry]
    ctx.record_input(
        "chain_snapshot", "cboe_delayed", snaps_digest, as_of=ctx.now, count=len(written)
    )
    metrics["tickers_fetched"] = len(written)
    if skipped:
        metrics["tickers_skipped"] = skipped
    if tickers and not written:
        sample = ", ".join(f"{k} {v}" for k, v in list(skipped.items())[:3])
        failed["chain_snapshot"] = f"0/{len(tickers)} tickers ({sample})"
    elif not tickers:
        failed["chain_snapshot"] = "no tickers in scope"
    if written:
        parts.append(f"{len(written)}/{len(tickers)} chains")

    # -- 3. exchange symbol_data ----------------------------------------------
    rows = []
    total = 0
    market_errors: dict[str, str] = {}
    for mkt in markets:
        try:
            body = csv_get(cf.SYMBOL_DATA_URL.format(mkt=mkt))
            if len(body) > cfg.max_csv_bytes:
                msg = f"body {len(body)} bytes > max_csv_bytes {cfg.max_csv_bytes}"
                raise ValueError(msg)
            parsed = cf.parse_symbol_data_csv(body.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - per-market isolation
            market_errors[mkt] = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        total += len(parsed)
        agg = cf.aggregate_by_underlying(parsed, mkt)
        rows.extend(cf.select_exchange_rows(agg, tickers))
    if markets and len(market_errors) == len(markets):
        failed["exchange_volume"] = "; ".join(f"{k}: {v}" for k, v in market_errors.items())
    else:
        ev = ExchangeVolumePayload(fetched_at=fetched_at, rows=rows[:60], total_rows_parsed=total)
        ctx.record_input(
            "exchange_volume",
            "cboe_symbol_data",
            [[r.market, r.underlying, r.volume] for r in ev.rows],
            as_of=ctx.now,
            count=total,
        )
        ctx.write("exchange_volume", "market", ev)
        parts.append(f"{total:,} exchange rows")
        metrics["exchange_rows_parsed"] = total
    if market_errors:
        metrics["exchange_market_errors"] = market_errors

    metrics["failed_parts"] = sorted(failed)
    metrics["parts_ok"] = 3 - len(failed)
    metrics["duration_s"] = round(time.perf_counter() - t0, 2)
    if len(failed) == 3:
        msg = "options_fast: all parts failed: " + "; ".join(f"{k}: {v}" for k, v in failed.items())
        raise RuntimeError(msg)
    summary = " · ".join(parts)
    if failed:
        summary += " · failed: " + ", ".join(sorted(failed))
    return JobResult(summary=summary, metrics=metrics)


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
    """Today's active list (D51) + open-position underlyings (E12.4) + today's
    candidates: what the chain may trade or must manage."""
    from arc.universe.tiers import active_tickers, open_underlyings

    tickers = [
        str(t).upper()
        for t in ctx.options.get("tickers") or active_tickers(ctx.conn, ctx.settings, ctx.now)
    ]
    rows = ctx.conn.execute(
        "SELECT DISTINCT ticker FROM candidates WHERE day = ?",
        (ctx.now.astimezone(ET).date().isoformat(),),
    ).fetchall()
    return list(dict.fromkeys([*tickers, *open_underlyings(ctx.conn), *(str(r[0]) for r in rows)]))


def iv_record_source(
    ctx: JobContext,
    market: MarketDataProvider | None = None,
    cboe_get: Callable[[str], bytes] | None = None,
) -> JobResult:
    """E4.12 (D55): today's 30-DTE IV per ticker -> ``iv_daily`` (``alpaca_cm30``),
    cross-checked against Cboe ``iv30`` for SPY, QQQ and a few pool names.

    No context kind is written (``writes: []``): the regime step reads ``iv_daily``.
    A cross-check breach is flagged on the row; the monitor's ``iv_crosscheck`` check
    opens the [Ops] alert. Diffs are in the run's ``metrics.crosscheck``.
    """
    from arc.iv.record import record_day

    s = ctx.settings
    if market is None:  # pragma: no cover - live Alpaca (integration)
        from arc.data.alpaca import AlpacaMarketData

        market = AlpacaMarketData()
    if cboe_get is None:  # pragma: no cover - live Cboe
        from arc.ingest.options_data import http_get

        def cboe_get(url: str) -> bytes:
            return http_get(url, "Mozilla/5.0 (Project Arc)", timeout=20.0, retries=1)

    tickers = _data_tickers(ctx)
    res = record_day(
        ctx.conn,
        market,
        tickers,
        ctx.now.astimezone(ET).date(),
        now=ctx.now,
        max_spread_pct=s.spot_max_spread_pct,
        cboe_get=cboe_get,
        crosscheck_max_pts=s.iv_crosscheck_max_pts,
        crosscheck_max_names=s.iv_crosscheck_max_names,
    )
    _data_result(
        ctx,
        "iv_daily",
        "alpaca",
        [{"ticker": r.ticker, "iv30": r.iv30, "spot": r.spot} for r in res.rows],
        len(res.rows),
    )
    if not res.rows and res.errors:
        msg = f"no IV recorded ({len(res.errors)} errors, e.g. {next(iter(res.errors.items()))})"
        raise RuntimeError(msg)
    checks = " · ".join(
        f"{c.ticker} {c.ours * 100:.1f}/{'n/a' if c.cboe is None else f'{c.cboe * 100:.1f}'}"
        for c in res.checks
    )
    return JobResult(
        summary=(
            f"{len(res.rows)} tickers recorded"
            + (f" · {len(res.errors)} errors" if res.errors else "")
            + (f" · ours/Cboe iv30 {checks}" if checks else "")
            + (f" · BREACH {', '.join(c.ticker for c in res.breaches)}" if res.breaches else "")
        ),
        metrics=res.metrics(),
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

    tickers = ctx.options.get("tickers")
    reg = SourceRegistry.from_routines(ctx.routines)
    max_age = reg.max_age_for(ctx.job) if ctx.job in reg.sources else None
    return _source_result(
        ctx,
        fetch_edgar(
            ctx.conn,
            ctx.settings,
            now=ctx.now,
            max_age=max_age,
            tickers=[str(t).upper() for t in tickers] if tickers else None,
        ),
    )


EARNINGS_NO_KEY_DAY = "earnings:no_api_key_notice"


def _alpaca_news_get(ctx: JobContext) -> Any:  # pragma: no cover - live keys
    """The Alpaca news getter, or ``None`` when no Alpaca key is set (``no_api_key``)."""
    from arc.data.alpaca import _get_keys
    from arc.ingest.ticker_news import alpaca_getter

    try:
        key, secret = _get_keys()
    except RuntimeError:
        return None
    return alpaca_getter(key, secret)


def _finnhub_news_client(ctx: JobContext) -> Any:
    """Finnhub client on the shared 55/min budget, or ``None`` without a key."""
    from arc.ingest.finnhub import DbRateLimiter, FinnhubClient, FinnhubNoKey

    s = ctx.settings
    try:
        return FinnhubClient(
            s.finnhub_api_key,
            limiter=DbRateLimiter(ctx.conn, calls_per_minute=s.finnhub_calls_per_minute),
        )
    except FinnhubNoKey:
        return None


def ticker_news_source(
    ctx: JobContext,
    *,
    alpaca_get: Any = "live",
    finnhub_client: Any = "live",
    sleep: Callable[[float], None] | None = None,
) -> JobResult:
    """E14.1 (D60): ticker-tagged news (Alpaca/Benzinga, Finnhub fallback) for the scope.

    Scope = active list ∪ open underlyings (``tickers: active_list``, capped at
    ``max_tickers``). Outcomes: nothing answered and every reason ``no_api_key`` ->
    ``skipped`` (``no_api_key``); nothing answered otherwise -> ``failed``
    (``forbidden`` / ``rate_limited`` / ``error``); some tickers answered -> ``ok``
    with ``failed_tickers``. Never ``ok · 0`` for a run no provider answered.
    """
    import datetime as dt
    import time

    from arc.ingest.sources import SourceRegistry
    from arc.ingest.ticker_news import fetch_ticker_news, source_key
    from arc.ingest.ticker_news_config import TickerNewsConfig

    cfg = TickerNewsConfig.from_options(ctx.options)
    scope = _options_fast_tickers(ctx, int(ctx.options.get("max_tickers", 50)))
    if not scope:
        msg = "ticker_news: empty scope (no active list, no open underlyings)"
        raise JobSkippedError(msg)
    reg = SourceRegistry.from_routines(ctx.routines)
    keys = [source_key(n) for n in cfg.enabled if source_key(n) in reg.sources]
    window = reg.max_age_for(keys[0]) if keys else None
    max_age = (
        window.duration
        if window is not None and window.duration is not None
        else dt.timedelta(hours=12)
    )
    fetched = fetch_ticker_news(
        ctx.conn,
        cfg,
        scope,
        now=ctx.now,
        max_age=max_age,
        alpaca_get=_alpaca_news_get(ctx) if alpaca_get == "live" else alpaca_get,
        finnhub_client=(_finnhub_news_client(ctx) if finnhub_client == "live" else finnhub_client),
        sleep=sleep or time.sleep,
        run_id=ctx.run_id,
    )
    for name, run in fetched.inputs.items():
        ctx.record_input(
            f"ticker_news.{name}",
            run.type,
            {"tickers": run.tickers, "status": run.status},
            as_of=ctx.now,
            count=run.articles,
        )
    unresolved = fetched.unresolved
    answered = [t for t in fetched.scope if t not in unresolved]
    if not answered:
        # every input's reasons: a keyless fallback never hides the primary's 403/429
        reasons = {r for run in fetched.inputs.values() for r in run.failed.values()}
        detail = "; ".join(
            f"{n}: {r.status} ({'; '.join(r.errors[:2]) or ','.join(sorted(r.reasons))})"
            for n, r in fetched.inputs.items()
            if r.status != "not_needed"
        )
        if reasons <= {"no_api_key"}:
            msg = f"no_api_key: no ticker_news input has a key ({detail})"
            raise JobSkippedError(msg)
        reason = next((r for r in ("forbidden", "rate_limited", "error") if r in reasons), "error")
        msg = f"ticker_news {reason}: no input answered ({detail})"
        raise RuntimeError(msg)
    res = _source_result(ctx, fetched.docs)
    per_input = ", ".join(f"{n} {fetched.new[n]}" for n in fetched.inputs)
    n_filtered = sum(fetched.filtered.values())
    covered = sorted(fetched.seen_tickers)
    summary = f"{res.summary} ({per_input}), {n_filtered} filtered"
    summary += f" · {len(covered)}/{len(fetched.scope)} names"
    if unresolved:
        summary += f" · {len(unresolved)} failed ({', '.join(sorted(unresolved)[:5])})"
    metrics: dict[str, Any] = {
        **res.metrics,
        "filtered": n_filtered,
        "duplicates": fetched.duplicates,
        "stale": fetched.stale,
        "scope": len(fetched.scope),
        "covered": len(covered),
        "covered_tickers": covered,
        "per_ticker": dict(sorted(fetched.per_ticker.items())),
        "inputs": {n: r.status for n, r in fetched.inputs.items()},
        "calls": {n: r.calls for n, r in fetched.inputs.items()},
        "failed_tickers": dict(sorted(unresolved.items())),
    }
    metrics.update({f"new_{n}": fetched.new[n] for n in fetched.inputs})
    metrics.update({f"filtered_{n}": fetched.filtered[n] for n in fetched.inputs})
    return JobResult(summary=summary, metrics=metrics)


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
    from arc.universe.tiers import TIER_ORDER, active_by_tier

    universe = IngestUniverse.from_settings(s, now=ctx.now, conn=ctx.conn)
    # E12.4: open underlyings -> today's candidates -> core -> momentum -> discovery
    # (today's active list by tier); a `tickers` job option replaces the tier part.
    if ctx.options.get("tickers"):
        tiers: dict[str, list[str]] = {"tickers": list(ctx.options["tickers"])}
    else:
        by_tier = active_by_tier(ctx.conn, s, ctx.now)
        tiers = {t.value: by_tier.get(t, []) for t in TIER_ORDER}
    scope = ticker_scope(
        ctx.conn,
        tiers=tiers,
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


def resolve_universe(ctx: JobContext, *, write: Callable[..., Any] | None = None) -> ActiveUniverse:
    """D51: resolve today's active list (no network) and write ``active_universe``.

    Overflow past ``universe_active_max`` is journaled ``universe:over_active_cap``.
    *write* (E12.2) replaces ``ctx.write``, e.g. to cap the entry's TTL.
    """
    from arc.universe.tiers import build_active, record_active

    active, _ = build_active(ctx.conn, ctx.settings, ctx.now)
    ctx.record_input(
        "active_universe", "tiers", active.tickers, as_of=ctx.now, count=len(active.members)
    )
    record_active(
        ctx.conn,
        active,
        at=ctx.now,
        write=write or ctx.write,
        run_id=ctx.run_id,
        chain_run_id=ctx.chain_run_id,
    )
    return active


def symbols_source(ctx: JobContext) -> JobResult:
    """Pre-market universe job (E5.7 / D28, D51).

    Every trading day: resolves the D51 active list (``active_universe``, no
    network). On the ``refresh_days`` (Mondays), or when the cache is missing or
    older than ``symbol_master.refresh_days``: refreshes the symbol master (SEC
    tickers ∪ Alpaca optionable), the only scheduled writer of the cache that
    ingest and the Scalp read (they never fetch mid-run).
    """
    from arc.universe import load_symbol_master, load_universe_config, refresh_symbol_master

    cfg = load_universe_config(ctx.settings.universe_config_file)
    weekday = ctx.now.astimezone(ET).strftime("%a").lower()
    refresh_on = [str(d).lower() for d in ctx.options.get("refresh_days", ["mon"])]
    cached = load_symbol_master(
        cfg.symbol_master,
        user_agent=ctx.settings.edgar_user_agent,
        now=ctx.now,
        fetch_if_missing=False,
    )
    due = (
        weekday in refresh_on
        or cached is None
        or cached.is_stale(ctx.now, cfg.symbol_master.refresh_days)
    )
    metrics: dict[str, Any] = {}
    parts: list[str] = []
    if due:
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
        parts.append(f"{len(master.symbols)} symbols ({optionable} optionable)")
        metrics |= {"symbols": len(master.symbols), "optionable": optionable, **master.sources}
    active = resolve_universe(ctx)
    c = active.counts
    parts.append(
        f"active {len(active.members)} (core {c['core']} · momentum {c['momentum']} · "
        f"discovery {c['discovery']} · trending {c.get('trending', 0)})"
    )
    metrics |= {"active": len(active.members), **{f"tier_{k}": v for k, v in c.items()}}
    return JobResult(summary=" · ".join(parts), metrics=metrics)


MOMENTUM_SOURCES_DEFAULT = ("stockanalysis", "schwab")
#: E12.2: rows the momentum feed keeps (the tier takes its top universe_momentum_size_d56).
MOMENTUM_FEED_SIZE = 25


def run_momentum(
    *,
    settings: ArcSettings,
    options: Mapping[str, Any],
    now: _dt.datetime,
    get: Callable[[str], bytes] | None = None,
) -> Any:
    """E12.2: fetch + select the momentum tier (no write). Returns a ``MomentumFetch``.

    Raises :class:`arc.universe.momentum.MomentumError` when every source fails.
    """
    from arc.pipeline.market import ETF_UNDERLYINGS
    from arc.universe import load_symbol_master, load_universe_config
    from arc.universe.momentum import fetch_momentum

    cfg = load_universe_config(settings.universe_config_file)
    master = load_symbol_master(
        cfg.symbol_master, user_agent=settings.edgar_user_agent, now=now, fetch_if_missing=False
    )
    order = [str(s) for s in options.get("source_order", MOMENTUM_SOURCES_DEFAULT)]
    size = int(options.get("size", MOMENTUM_FEED_SIZE))
    return fetch_momentum(
        cfg.momentum,
        source_order=order,
        size=size,
        user_agent=settings.edgar_user_agent,
        master=master,
        etfs=ETF_UNDERLYINGS | set(cfg.tiers.reference()),
        get=get,
    )


def universe_momentum_source(ctx: JobContext) -> JobResult:
    """E12.2 / D51: monthly S&P 500 Momentum top N (SPMO holdings) -> ``universe_tier``.

    Writes the ``momentum`` tier entry (TTL from the job's ``context:`` policy, 35d),
    records the fetched page's digest + URL + as-of in the run manifest (D27), posts
    the monthly diff as a notice, then re-resolves today's active list. Every source
    failing raises: the run is ``failed`` + alerted, nothing is written, and the
    previous entry stays valid until its TTL.
    """
    import datetime as dt

    from arc.universe import load_universe_config
    from arc.universe.momentum import (
        build_payload,
        is_stale,
        notice_line,
        previous_members,
        tier_diff,
    )

    fetch = run_momentum(settings=ctx.settings, options=ctx.options, now=ctx.now)
    today = ctx.now.astimezone(ET).date()
    cfg = load_universe_config(ctx.settings.universe_config_file).momentum
    stale = is_stale(fetch.as_of, today, cfg.stale_after_days)
    as_of_dt = dt.datetime.combine(fetch.as_of, dt.time(), tzinfo=ET) if fetch.as_of else None
    ctx.record_input(
        "spmo_holdings", fetch.url, None, as_of=as_of_dt, count=len(fetch.rows), digest=fetch.digest
    )
    previous = previous_members(ctx.conn)
    order = [str(s) for s in ctx.options.get("source_order", MOMENTUM_SOURCES_DEFAULT)]
    line = notice_line(fetch, previous, stale=stale, primary=order[0] if order else "")
    ctx.write("universe_tier", "momentum", build_payload(fetch, now=ctx.now))
    # The job's `context:` (35d) is for the tier entry; the active list keeps its own
    # `context_ttl` (1 session), so tomorrow's runs re-resolve it.
    active_ttl = ctx.routines.context_ttl.get("active_universe")

    def write_active(kind: str, subject: str, payload: Any, **kw: Any) -> Any:
        return ctx.write(kind, subject, payload, ttl=active_ttl.ttl if active_ttl else None, **kw)

    active = resolve_universe(ctx, write=write_active)
    added, removed = tier_diff(previous or [], fetch.tickers)
    return JobResult(
        summary=f"{line} · active {len(active.members)}",
        notice=line,
        metrics={
            "names": len(fetch.picks),
            "rows": len(fetch.rows),
            "source": fetch.source,
            "source_as_of": fetch.as_of.isoformat() if fetch.as_of else None,
            "stale": stale,
            "partial": fetch.partial,
            "added": added,
            "removed": removed,
            "dropped": [f"{s}:{r}" for s, r in fetch.dropped],
            "source_errors": fetch.errors,
            "active": len(active.members),
        },
    )


def retail_buzz_source(
    ctx: JobContext, *, get: Callable[[str, float, int], bytes] | None = None
) -> JobResult:
    """E13.19 (D58): daily Reddit (ApeWisdom) + Stocktwits pull -> one ``retail_buzz``
    entry (subject ``all``) with each input's raw rows. Inputs are independent: a
    failed one is recorded ``failed`` and contributes nothing; every input failing
    raises (``failed`` + alerted, nothing written)."""
    from arc.ingest.retail_buzz import fetch_retail_buzz, http_getter
    from arc.ingest.retail_buzz_config import RetailBuzzConfig

    cfg = RetailBuzzConfig.from_options(ctx.options)
    payload = fetch_retail_buzz(
        cfg, now=ctx.now, get=get or http_getter(ctx.settings.edgar_user_agent)
    )
    for name, inp in payload.inputs.items():
        ctx.record_input(
            f"retail_buzz.{name}",
            " ".join(inp.urls) or inp.type,
            None,
            as_of=ctx.now,
            count=len(inp.rows),
            digest=inp.digest or None,
        )
    live = payload.live
    if not live:
        detail = "; ".join(f"{n}: {i.error or i.status}" for n, i in payload.inputs.items())
        msg = f"retail_buzz: no input answered ({detail})"
        raise RuntimeError(msg)
    ctx.write("retail_buzz", "all", payload)
    counts = {n: len(i.rows) for n, i in payload.inputs.items()}
    failed = [n for n in payload.inputs if n not in live]
    summary = " · ".join(f"{n} {c}" for n, c in counts.items())
    if failed:
        summary += " · no data: " + ", ".join(failed)
    return JobResult(
        summary=summary,
        metrics={
            "inputs": {n: i.status for n, i in payload.inputs.items()},
            "rows": counts,
            "input_errors": {n: payload.inputs[n].error for n in failed},
        },
    )


def run_trending_tier(
    *,
    conn: sqlite3.Connection,
    settings: ArcSettings,
    routines: RoutinesConfig,
    options: Mapping[str, Any],
    now: _dt.datetime,
    screen: bool = True,
    market_factory: Callable[[], Any] | None = None,
) -> Any:
    """E13.19 (D58): rank the latest ``retail_buzz`` entry into the trending tier
    (no write). Returns a ``TrendingResult``; raises ``TrendingError`` when the tier
    cannot be built (0 live inputs, no symbol master)."""
    from datetime import timedelta

    from arc.context.categories import SourceCategory
    from arc.ingest.retail_buzz_config import RetailBuzzConfig
    from arc.universe import load_symbol_master
    from arc.universe.config import universe_config
    from arc.universe.guard import UniverseGuard
    from arc.universe.tiers import Tier, market_reference, tier_membership
    from arc.universe.trending import (
        TrendingOptions,
        latest_buzz,
        normalise_exclusions,
        run_trending,
    )

    opts = TrendingOptions.from_options(options)
    buzz_spec = routines.sources.get("retail_buzz")
    enabled = (
        list(RetailBuzzConfig.from_options(buzz_spec.options).enabled)
        if buzz_spec is not None
        else []
    )
    max_age = routines.source_max_age("retail_buzz", SourceCategory.RETAIL_BUZZ).duration
    buzz, stale = latest_buzz(conn, now=now, max_age=max_age or timedelta(hours=24))
    ucfg = universe_config(settings)
    master = load_symbol_master(
        ucfg.symbol_master, user_agent=settings.edgar_user_agent, now=now, fetch_if_missing=False
    )
    exclude: dict[str, str] = {
        t: tier.value
        for t, tier in tier_membership(conn, settings, now).items()
        if tier in (Tier.CORE, Tier.MOMENTUM, Tier.DISCOVERY)
    }
    if opts.exclude_market_reference:
        for t in market_reference(settings):
            exclude.setdefault(t, "market_reference")
    exclude = normalise_exclusions(exclude, ucfg.momentum.share_class_aliases)
    guard_screen: Callable[[str], Any] | None = None
    if screen:
        guard = UniverseGuard.from_settings(
            settings,
            now=now,
            master=master,
            config=ucfg,
            conn=conn,
            market_factory=market_factory,
        )
        profile = ucfg.tier_screen("trending")
        if profile != "none":
            guard_screen = lambda sym: guard.screen(sym, profile)  # noqa: E731
    return run_trending(
        buzz,
        enabled=enabled,
        stale=stale,
        opts=opts,
        now=now,
        master=master,
        size=settings.universe_trending_size,
        exclude=exclude,
        screen=guard_screen,
    )


def universe_trending_source(ctx: JobContext) -> JobResult:
    """E13.19 (D58): daily trending tier from the ``retail_buzz`` pull -> ``universe_tier``
    (subject ``trending``), then re-resolve today's active list.

    Journals every admission / screen fail / leveraged drop and posts the daily diff as
    a notice. 0 live inputs raises: ``failed`` + alerted, nothing written, the tier is
    empty today (yesterday's entry has expired).
    """
    from arc.universe.trending import (
        build_payload,
        journal_decisions,
        notice_line,
        previous_members,
    )

    res = run_trending_tier(
        conn=ctx.conn,
        settings=ctx.settings,
        routines=ctx.routines,
        options=ctx.options,
        now=ctx.now,
    )
    for i in res.inputs:
        ctx.record_input(
            f"trending.{i.name}",
            " ".join(i.urls) or i.type,
            sorted(i.raw),
            as_of=ctx.now,
            count=i.count,
            digest=i.digest or None,
        )
    previous = previous_members(ctx.conn)
    line = notice_line(res, previous)
    journaled = journal_decisions(
        ctx.conn, res, at=ctx.now, run_id=ctx.run_id, chain_run_id=ctx.chain_run_id
    )
    ctx.write("universe_tier", "trending", build_payload(res, now=ctx.now))
    active = resolve_universe(ctx)
    prev = set(previous or [])
    kept = sum(1 for m in active.members if m.tier.value == "trending")
    return JobResult(
        summary=f"{line} · active {len(active.members)} ({kept} trending)",
        notice=line,
        metrics={
            "names": len(res.members),
            "both_inputs": sum(1 for r in res.members if r.n_inputs >= 2),
            "tickers": res.tickers,
            "added": [t for t in res.tickers if t not in prev],
            "removed": [t for t in (previous or []) if t not in set(res.tickers)],
            "ranked": len(res.ranked),
            "pool": len(res.pool),
            "screen_fail": sum(1 for r in res.pool if r.screen_passed is False),
            "excluded": len(res.excluded),
            "leveraged": [r.ticker for r in res.leveraged],
            "inputs": {i.name: i.status for i in res.inputs},
            "input_errors": res.failed_inputs,
            "journaled": journaled,
            "active": len(active.members),
            "active_trending": kept,
        },
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
    llm: PersonaLLM | None = None,
    *,
    session: Any = None,
    list_videos: Callable[[str, int], list[dict[str, Any]]] | None = None,
    fetch_info: Callable[[str], Mapping[str, Any]] | None = None,
    price_lookup: Any = _UNSET,
) -> JobResult:
    """E4.6 (D45): the daily 02:00 ET YouTube brief run, all channels in one job.

    Per channel: newest qualifying video in ``lookback`` -> transcript -> brief ->
    ``channel_brief`` context entry (subject ``youtube.<slug>``, supersede latest)
    plus a ``raw_doc_ref``. D60: the entry's ``valid_from`` is the video's publish
    time, so the job's ``context`` TTL and the category ``max_age`` both run from
    publish: a 47h-old video's brief is read and a 49h-old one's is not. The keyword
    arguments replace the network pieces in tests.
    """
    from pathlib import Path

    from arc.context.kinds import ChannelBriefPayload, RawDocRefPayload
    from arc.ingest.channels import CHANNELS_DIR, ChannelRegistry, default_registry
    from arc.ingest.channels.daily import DailyBriefConfig, run_daily_briefs
    from arc.ingest.llm import HermesScalpLLM
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
            valid_from=min(cr.brief.published_at, now),  # D60: ages run from publish
        )

    run = run_daily_briefs(
        ctx.conn,
        settings,
        cfg,
        llm=llm or HermesScalpLLM.from_settings(settings),
        session=session,
        now=now,
        ttl=ttl,
        universe=IngestUniverse.from_settings(settings, now=now, conn=ctx.conn),
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


_MENTIONS_LINE_CHARS = 300


def scalp_mentions_line(result: ScalpRunResult) -> str:
    """D56: ``Outside the universe: X (bullish, earnings), Y (bearish)`` (<= 300 chars)."""
    if not result.mentions:
        return ""
    parts = [
        f"{m.ticker} ({m.stance.value}"
        + (f", {m.catalyst_type.value})" if m.catalyst_type else ")")
        for m in result.mentions
    ]
    line = "Outside the universe: " + ", ".join(parts)
    while len(line) > _MENTIONS_LINE_CHARS and len(parts) > 1:
        parts.pop()  # drop whole items, never cut one in half
        line = "Outside the universe: " + ", ".join(parts) + ", …"
    return line[:_MENTIONS_LINE_CHARS]


def scalp_tape_line(result: ScalpRunResult) -> str:
    """E13.10: one line on the options tape this run read ("" with the flag off)."""
    tape = result.tape
    if tape is None:
        return ""
    if not tape.present:
        return tape.text  # "Options tape: no fresh info (age …)"
    vix = f"VIX {tape.vix:.1f}" if tape.vix is not None else "VIX n/a"
    ratio = f" · 9D/30D {tape.vix9d / tape.vix:.2f}" if tape.vix9d is not None and tape.vix else ""
    n = len(tape.tickers)
    line = f"Options tape: {vix}{ratio} · {n} ticker{'s' if n != 1 else ''}"
    if result.tape_corroborated:
        line += f" · corroborated {', '.join(sorted(result.tape_corroborated))}"
    return line


def _scalp_note(ctx: JobContext, result: ScalpRunResult, about: list[str]) -> None:
    """One ``observation`` note per scalp run from the batches' ``scan_summary`` (D27).

    E13.10: plus one options-tape line (flag on) and the D56 out-of-tier mentions line;
    ``facts["mentions"]`` carries the mention count.
    """
    from pydantic import ValidationError

    from arc.context.kinds import Evidence, NotePayload, NoteTopic

    if not result.summaries:
        return
    from arc.context.kinds import NOTE_SECTION_MAX, NoteSection

    urls = list(dict.fromkeys(result.summary_sources))[:20]
    # E13.13 (note v5): bold sections instead of one free-text body.
    pairs = [
        ("Summary", "\n\n".join(result.summaries)),
        ("Options sentiment", scalp_tape_line(result)),
        ("Themes", scalp_mentions_line(result)),
    ]
    sections = [
        NoteSection(label=label, text=text.strip()[:NOTE_SECTION_MAX])  # type: ignore[arg-type]
        for label, text in pairs
        if text and text.strip()
    ]
    facts: dict[str, str | float | int | bool] = {"docs": result.docs_scalped}
    if result.mentions:
        facts["mentions"] = len(result.mentions)
    if result.tape is not None:
        facts["tape_tickers"] = result.tape_tickers
        facts["tape_corroborated"] = len(result.tape_corroborated)
    try:
        payload = NotePayload(
            persona="scalp",
            topic=NoteTopic.OBSERVATION,
            title=f"Scan summary ({result.docs_scalped} docs)",
            sections=sections,
            about=about,
            evidence=[Evidence(ref=u) for u in urls],
            facts=facts,
        )
    except ValidationError as exc:
        log.warning("pipeline.note_invalid", persona="scalp", error=str(exc))
        return
    ctx.write("note", "market", payload)


def _journal_universe_rejects(ctx: JobContext, result: ScalpRunResult) -> int:
    """E7.4: one ``candidate``-stage decision per universe reject (D28). Returns the count."""
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore

    codes = {
        "not_in_universe": ReasonCode.UNIVERSE_NOT_IN_UNIVERSE,
        "unknown_symbol": ReasonCode.UNIVERSE_UNKNOWN_SYMBOL,
        "illiquid": ReasonCode.UNIVERSE_ILLIQUID,
        "not_in_tier": ReasonCode.UNIVERSE_NOT_IN_TIER,  # D56: a mention, never a candidate
    }
    store = JournalStore(ctx.conn)
    n = 0
    for key, code in codes.items():
        for ticker in dict.fromkeys(result.rejected_items.get(key, [])):
            store.record(
                persona=JournalPersona.SCALP,
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


def _journal_tape_corroborations(ctx: JobContext, result: ScalpRunResult) -> int:
    """E13.10: one ``tape_corroborated`` decision per candidate the options tape
    corroborated, once per ticker per ET day. Returns the count."""
    if not result.tape_corroborated:
        return 0
    import datetime as dt

    from arc.context.ttl import to_db
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore

    day = ctx.now.astimezone(ET).date()
    start = dt.datetime(day.year, day.month, day.day, tzinfo=ET)
    done = {
        r[0]
        for r in ctx.conn.execute(
            "SELECT subject FROM decisions WHERE reason_code = ? AND at >= ? AND at < ?",
            (
                ReasonCode.TAPE_CORROBORATED.value,
                to_db(start),
                to_db(start + dt.timedelta(days=1)),
            ),
        ).fetchall()
    }
    store = JournalStore(ctx.conn)
    tape = result.tape
    n = 0
    for ticker, pc in sorted(result.tape_corroborated.items()):
        if ticker in done or tape is None:
            continue
        t = tape.tickers[ticker]
        store.record(
            persona=JournalPersona.SCALP,
            stage=Stage.CANDIDATE,
            subject=ticker,
            choice=Choice.SELECTED,
            reason_code=ReasonCode.TAPE_CORROBORATED,
            reason_text=f"session P/C {pc:.2f} reads {t.direction}" if pc is not None else "",
            at=ctx.now,
            chain_run_id=ctx.chain_run_id,
            run_id=ctx.run_id,
            payload={
                "direction": t.direction,
                "pc_volume": pc,
                "atm_spread_pct": t.atm_spread_pct,
                "atm_oi": t.atm_oi,
                "tape_as_of": tape.as_of,
            },
        )
        n += 1
    return n


def _journal_floor_rejects(ctx: JobContext, result: ScalpRunResult) -> int:
    """D56 (E13.4): one rejected ``universe:below_tier_floor`` decision per idea below
    its tier's confidence floor (payload ``confidence_floor_skipped: tier=<t> floor=<f>``).

    Never written as a candidate. Once per ticker per ET day. Returns the count.
    """
    if not result.floor_rejected:
        return 0
    import datetime as dt

    from arc.context.ttl import to_db
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore

    day = ctx.now.astimezone(ET).date()
    start = dt.datetime(day.year, day.month, day.day, tzinfo=ET)
    done = {
        r[0]
        for r in ctx.conn.execute(
            "SELECT subject FROM decisions WHERE reason_code = ? AND at >= ? AND at < ?",
            (
                ReasonCode.UNIVERSE_BELOW_TIER_FLOOR.value,
                to_db(start),
                to_db(start + dt.timedelta(days=1)),
            ),
        ).fetchall()
    }
    store = JournalStore(ctx.conn)
    n = 0
    for ticker, (tier, conf, floor) in sorted(result.floor_rejected.items()):
        if ticker in done:
            continue
        store.record(
            persona=JournalPersona.SCALP,
            stage=Stage.CANDIDATE,
            subject=ticker,
            choice=Choice.REJECTED,
            reason_code=ReasonCode.UNIVERSE_BELOW_TIER_FLOOR,
            reason_text=f"{tier} name below its tier floor ({conf:.2f} < {floor:.2f})",
            confidence=conf,
            at=ctx.now,
            chain_run_id=ctx.chain_run_id,
            run_id=ctx.run_id,
            payload={
                "confidence_floor_skipped": f"tier={tier} floor={floor:g}",
                "tier": tier,
                "confidence": conf,
                "floor": floor,
            },
        )
        n += 1
    return n


def scalp_persona(
    ctx: JobContext, llm: PersonaLLM | None = None, guard: UniverseGuard | None = None
) -> JobResult:
    """Scalp (E4.2): summarise unscalped docs; write each merged Candidate to context.

    *llm* overrides the Hermes backend and *guard* the D28 universe policy
    (``arc propose --fixtures``, tests).
    """
    from arc.context.kinds import CandidatePayload
    from arc.ingest.scalp import run_scalp

    kwargs: dict[str, Any] = {"now": ctx.now, "run_id": ctx.run_id, "routines": ctx.routines}
    if llm is not None:
        kwargs["llm"] = llm
    if guard is not None:
        kwargs["guard"] = guard
    write_stories = "story" in (ctx.spec.writes or [])  # D30 stage-1 digests
    if "active_universe" in (ctx.spec.writes or []):
        resolve_universe(ctx)  # D51: cheap, no network; the guard + prompt read it
    result = run_scalp(ctx.conn, ctx.settings, **kwargs)
    if write_stories:  # D47: each story expires at min(policy, freshest source max_age + 2h)
        for p in result.stories:
            ctx.write("story", p.story_id, p, ttl=result.story_ttls.get(p.story_id))
    _journal_universe_rejects(ctx, result)
    _journal_floor_rejects(ctx, result)
    _journal_tape_corroborations(ctx, result)
    if result.tape is not None:  # E13.10: the tape this run read (text + numbers)
        ctx.record_input(
            "options_tape",
            "options_fast",
            result.tape.model_dump(),
            as_of=ctx.now,
            count=result.tape_tickers,
        )
    written = [
        ctx.write(
            "candidate",
            cand.ticker,
            CandidatePayload.model_validate(cand.model_dump()),
            ttl=result.candidate_ttls.get(cand.ticker),
        ).id
        for cand in result.candidates
    ]
    _scalp_note(ctx, result, written)
    from arc.slack.digests import scalp_card

    return JobResult(
        summary=(
            f"{result.docs_scalped} docs ({len(result.stories)} stories) → "
            f"{result.accepted} accepted, {len(result.candidates)} candidates today"
            + (f", {result.over_budget} over budget" if result.over_budget else "")
            + (f", {result.filtered} filtered" if result.filtered else "")
            + (f", {result.failed_batches} failed batches" if result.failed_batches else "")
        ),
        metrics={
            "new_candidates": result.accepted,
            "candidates": len(result.candidates),
            "docs_scalped": result.docs_scalped,
            "stories": len(result.stories),
            "over_budget": result.over_budget,
            "skipped_budget": result.skipped_budget,
            "skipped_stale": result.skipped_stale,
            "slow_feed": result.slow_feed,
            "filtered": result.filtered,
            "digest_batches": result.digest_batches,
            "failed_digest_batches": result.failed_digest_batches,
            "failed_batches": result.failed_batches,
            "mentions": len(result.mentions),
            **(
                {
                    "tape_present": result.tape_present,
                    "tape_tickers": result.tape_tickers,
                    "tape_corroborated": len(result.tape_corroborated),
                }
                if result.tape is not None
                else {}
            ),
            **{f"source_{label}": read for label, read, _ in result.source_mix},
        },
        card=scalp_card(
            docs=result.docs_scalped,
            accepted=result.accepted,
            candidates=result.candidates,
            rejected=result.rejected,
            rejected_items=result.rejected_items,
            rationales=result.rationales,
            failed_batches=result.failed_batches,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
            reject_details=result.reject_details,
            source_mix=result.source_mix,
            stories=len(result.stories),
            category_mix=result.category_mix,
            filtered={
                registry_label(ctx, k): n for k, n in sorted(result.filtered_by_source.items())
            },
            mentions=result.mentions,
            tape_line=scalp_tape_line(result),
        ),
    )


# ---------------------------------------------------------------------------
# E13.7 (D56): the daily Scout persona
# ---------------------------------------------------------------------------

SCOUT_JOB = "scout"


def scout_persona(
    ctx: JobContext, llm: PersonaLLM | None = None, guard: UniverseGuard | None = None
) -> JobResult:
    """Scout (E13.7, D56): snapshot → prompt → one cheap LLM call → code rules → writes.

    Writes the ``scout_read`` (subject ``session``), the ``discovery`` tier (only from the
    YouTube calls, ``loose`` screen, ≤ ``funnel.scout.max_discovery``), re-resolves the
    active list, then writes a ``candidate`` (``feed=scout``) per call at or above its
    tier's floor. *llm* / *guard* override the Hermes backend and the universe guard
    (tests). The persona call is recorded in ``persona_calls`` either way.
    """
    import time
    from collections import Counter

    from pydantic import ValidationError

    from arc.context.kinds import (
        CandidatePayload,
        ScoutCategoryPresence,
        ScoutInputsPresence,
        ScoutReadPayload,
    )
    from arc.ingest.channels.daily import JOB as BRIEFS_JOB
    from arc.ingest.channels.daily import configured_channels
    from arc.ingest.llm import HermesScalpLLM, ScalpLLMError
    from arc.ingest.scalp import extract_json_object, parse_catalyst_date, store_candidate
    from arc.journal.models import PersonaCallMeta
    from arc.models import Candidate
    from arc.personas.schemas import ScoutOutput
    from arc.personas.scout import (
        build_scout_prompt,
        category_max_ages,
        discovery_members,
        scout_input_from_context,
        scout_rules,
        validate_calls,
    )
    from arc.pipeline.market import ETF_UNDERLYINGS
    from arc.pipeline.steps import _with_constraints
    from arc.pipeline.store import PersonaCallRepo, sha256
    from arc.slack.digests import TrendingFact, scout_card
    from arc.store.repos import CandidateRepo
    from arc.universe.guard import UniverseGuard
    from arc.universe.tiers import (
        Tier,
        TierMember,
        UniverseTierPayload,
        load_tier_inputs,
        market_reference,
        tier_floor,
        tier_membership,
    )

    funnel = ctx.routines.funnel.scout
    settings = ctx.settings
    found = ctx.routines.job(BRIEFS_JOB)
    channels = configured_channels(found[1].options if found else None)
    membership = tier_membership(ctx.conn, settings, ctx.now)
    higher = {t: tier.value for t, tier in membership.items() if tier in (Tier.CORE, Tier.MOMENTUM)}
    floor = settings.universe_floor_discovery
    # E13.20 (D58): today's trending tier (code-ranked, E13.19), read as context only
    trending = load_tier_inputs(ctx.conn, settings, ctx.now).trending
    trending_names = [m.ticker for m in sorted(trending, key=lambda m: m.rank)][
        : settings.universe_trending_size
    ]
    buzz_velocity = None
    if ctx.routines.scout_buzz_velocity.enabled:  # E14.5 (D60), strategy lane, default off
        from arc.personas.scout import BuzzVelocity
        from arc.universe.config import universe_config
        from arc.universe.trending import TrendingOptions

        tjob = ctx.routines.job("universe.trending")
        topts = TrendingOptions.from_options(tjob[1].options if tjob else {})
        buzz_velocity = BuzzVelocity(
            options=topts.velocity,
            stop_words=tuple(universe_config(settings).extraction.stop_words),
        )
    inp = scout_input_from_context(
        ctx.snapshot,
        channels=channels,
        budget_chars=settings.scout_video_chars,
        max_age=category_max_ages(ctx.routines),
        max_discovery=funnel.max_discovery,
        discovery_floor=floor,
        higher_tier=sorted(higher),
        trending=trending_names,
        now=ctx.now,
        buzz_velocity=buzz_velocity,
    )
    rules = scout_rules(inp)
    prompt = _with_constraints(build_scout_prompt(inp), rules, ScoutOutput)
    inputs = {"scout_input": inp.model_dump(mode="json"), "rules": rules}
    ctx.record_input("scout_prompt", "context", prompt, as_of=ctx.now, count=len(inp.briefs))
    backend: PersonaLLM = llm or HermesScalpLLM.from_settings(
        settings, "scout", timeout_seconds=settings.scout_timeout_seconds
    )
    repo = PersonaCallRepo(ctx.conn)
    started = time.monotonic()
    model = str(getattr(backend, "model", "unknown"))
    try:
        reply = backend.complete(prompt)
    except ScalpLLMError as exc:
        repo.insert(
            run_id=ctx.run_id,
            persona="scout",
            model=model,
            snapshot_id=ctx.snapshot.id,
            prompt=prompt,
            raw_response=None,
            status="llm_error",
            error=str(exc),
            at=ctx.now,
            meta=PersonaCallMeta(
                prompt_text=prompt,
                prompt_inputs=inputs,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
        msg = f"scout LLM call failed: {exc}"
        raise RuntimeError(msg) from exc
    meta = PersonaCallMeta(
        prompt_text=prompt,
        prompt_inputs=inputs,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
        cost_usd=reply.cost_usd,
    )
    try:
        out = ScoutOutput.model_validate(extract_json_object(reply.text))
    except (ValueError, ValidationError) as exc:
        repo.insert(
            run_id=ctx.run_id,
            persona="scout",
            model=reply.model,
            snapshot_id=ctx.snapshot.id,
            prompt=prompt,
            raw_response=reply.text,
            status="parse_error",
            error=str(exc)[:2000],
            at=ctx.now,
            meta=meta,
        )
        msg = f"scout reply does not match ScoutOutput: {str(exc)[:300]}"
        raise RuntimeError(msg) from exc

    # -- code rules ---------------------------------------------------------
    calls, dropped_calls = validate_calls(out, inp.origins, max_calls=settings.scout_max_calls)
    if guard is None:
        guard = UniverseGuard.from_settings(settings, now=ctx.now, conn=ctx.conn)
    excluded = frozenset(ETF_UNDERLYINGS | set(market_reference(settings)))
    disc = discovery_members(
        out,
        calls,
        higher_tier=higher,
        excluded=excluded,
        floor=floor,
        max_discovery=funnel.max_discovery,
        screen=lambda sym: guard.screen(sym, "loose"),
    )
    dropped: Counter[str] = Counter(r.split(":", 1)[0] for r in dropped_calls.values())
    dropped.update(r.split(":", 1)[0] for r in disc.screened_out.values())
    PersonaCallRepo(ctx.conn).insert(
        run_id=ctx.run_id,
        persona="scout",
        model=reply.model,
        snapshot_id=ctx.snapshot.id,
        prompt=prompt,
        raw_response=reply.text,
        status="ok",
        dropped=dict(dropped),
        at=ctx.now,
        meta=meta,
        commit=False,
    )

    def presence(cat: str) -> ScoutCategoryPresence:
        return ScoutCategoryPresence(
            present=len(inp.present.get(cat, [])),
            configured=inp.configured.get(cat, 0),
            missing=inp.missing.get(cat, []),
        )

    read = ScoutReadPayload(
        as_of=ctx.now.astimezone(ET).isoformat(),
        session=inp.session,
        regime=out.regime,
        options_sentiment=out.options_sentiment,
        themes=out.themes,
        risks=out.risks,
        ticker_calls=calls,
        inputs=ScoutInputsPresence(
            youtube_macro=presence("youtube_macro"),
            youtube_micro=presence("youtube_micro"),
            options_daily=inp.options_as_of.get("options_daily"),
            vx_curve=inp.options_as_of.get("vx_curve"),
            vol_term=inp.options_as_of.get("vol_term"),
            retail_buzz=inp.retail_buzz.as_of if inp.retail_buzz is not None else None,
        ),
        discovery=disc.tickers,
        discovery_fill=len(disc.tickers),
        screened_out=disc.screened_out,
        prompt_sha=sha256(prompt),
        model=reply.model,
    )
    read_entry = ctx.write("scout_read", "session", read)

    # -- discovery tier + active list --------------------------------------
    today = ctx.now.astimezone(ET).date()
    members = [
        TierMember(
            ticker=c.ticker,
            tier=Tier.DISCOVERY,
            rank=i,
            source="scout",
            reason=f"{c.stance.value} · {', '.join(c.origins)} · score {c.confidence:.2f}",
            as_of=today,
        )
        for i, c in enumerate(disc.members, 1)
    ]
    ctx.write(
        "universe_tier",
        Tier.DISCOVERY.value,
        UniverseTierPayload(
            tier=Tier.DISCOVERY,
            members=members,
            fetched_at=ctx.now,
            source="scout",
            digest=sha256(",".join(disc.tickers)),
        ),
    )
    active = resolve_universe(ctx)

    # -- candidates (feed=scout) -------------------------------------------
    membership = tier_membership(ctx.conn, settings, ctx.now)  # discovery now written
    cand_repo = CandidateRepo(ctx.conn)
    written: list[str] = []
    written_tickers: list[str] = []
    below: dict[str, tuple[str, float, float]] = {}
    for c in calls:
        tier = membership.get(c.ticker)
        if tier is None:
            continue  # in no tier: the Scout's call is a mention only (D56)
        tfloor = tier_floor(settings, tier) or floor
        if c.confidence < tfloor:
            below[c.ticker] = (tier.value, c.confidence, tfloor)
            continue
        cand = Candidate(
            ticker=c.ticker,
            stance=c.stance,
            catalyst_type=c.catalyst_type,
            catalyst_date=parse_catalyst_date(c.catalyst_date),
            confidence=c.confidence,
            sources=list(c.origins),
            created_at=ctx.now,
        )
        stored = store_candidate(
            cand_repo, cand, day=today.isoformat(), run_id=ctx.run_id, source_key_of=str
        )
        payload = CandidatePayload.model_validate(
            {**stored.model_dump(), "feed": "scout", "origins": list(c.origins)}
        )
        written.append(ctx.write("candidate", c.ticker, payload).id)
        written_tickers.append(c.ticker)
    _scout_journal(ctx, read_entry.id, disc, calls, below, written_tickers=written_tickers)
    _scout_note(ctx, read, inp.session, [read_entry.id, *written])
    under = read.discovery_fill < funnel.min_discovery_alert
    return JobResult(
        summary=(
            f"briefs macro {read.inputs.youtube_macro.present}/"
            f"{read.inputs.youtube_macro.configured} · micro "
            f"{read.inputs.youtube_micro.present}/{read.inputs.youtube_micro.configured} → "
            f"{len(calls)} calls, discovery {read.discovery_fill}/{funnel.max_discovery}, "
            f"{len(written)} candidates · active {len(active.members)}"
            + (f" · under-filled (< {funnel.min_discovery_alert})" if under else "")
        ),
        metrics={
            "ticker_calls": len(calls),
            "dropped_calls": len(dropped_calls),
            "discovery_fill": read.discovery_fill,
            "max_discovery": funnel.max_discovery,
            "min_discovery_alert": funnel.min_discovery_alert,
            "under_filled": under,
            "candidates": len(written),
            "below_floor": len(below),
            "briefs": len(inp.briefs),
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "cost_usd": reply.cost_usd,
            "active": len(active.members),
            "retail_buzz": inp.retail_buzz is not None,
            "trending": len(trending_names),
        },
        card=scout_card(
            read=read,
            max_discovery=funnel.max_discovery,
            min_discovery_alert=funnel.min_discovery_alert,
            candidates=len(written),
            dropped_calls=dropped_calls,
            run_id=ctx.run_id,
            chain_run_id=ctx.chain_run_id,
            trending=TrendingFact(
                names=len(trending_names),
                size=settings.universe_trending_size,
                both=sum(
                    1 for m in trending if m.ticker in set(trending_names) and (m.inputs or 0) >= 2
                ),
                active=sum(1 for m in active.members if m.tier is Tier.TRENDING),
            ),
        ),
    )


def _scout_journal(
    ctx: JobContext,
    read_id: str,
    disc: Any,
    calls: list[Any],
    below: Mapping[str, tuple[str, float, float]],
    *,
    written_tickers: list[str],
) -> int:
    """E13.7: decisions for the Scout's discovery picks, screen drops, below-floor calls
    and candidates (``stage=candidate``, ``persona=scout``). Never commits."""
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import Recorder

    rec = Recorder(
        ctx.conn,
        at=ctx.now,
        chain_run_id=ctx.chain_run_id,
        run_id=ctx.run_id,
        inputs_snapshot_id=ctx.snapshot.id,
    )
    by = {c.ticker: c for c in calls}
    for rank, sym in enumerate(disc.tickers, 1):
        rec.add(
            JournalPersona.SCOUT,
            Stage.CANDIDATE,
            sym,
            Choice.SELECTED,
            ReasonCode.SCOUT_DISCOVERY,
            reason_text=f"discovery #{rank} · {', '.join(by[sym].origins)}",
            confidence=by[sym].confidence,
            payload={"rank": rank, "origins": list(by[sym].origins), "scout_read": read_id},
        )
    for sym, code, detail in disc.journal:
        rec.add(
            JournalPersona.SCOUT,
            Stage.CANDIDATE,
            sym,
            Choice.REJECTED,
            ReasonCode.SCOUT_DISCOVERY_SCREENED_OUT,
            reason_text=f"{code}: {detail}",
            payload={code: detail, "scout_read": read_id},
        )
    for sym, (tier, conf, tfloor) in sorted(below.items()):
        rec.add(
            JournalPersona.SCOUT,
            Stage.CANDIDATE,
            sym,
            Choice.REJECTED,
            ReasonCode.UNIVERSE_BELOW_TIER_FLOOR,
            reason_text=f"{tier} floor {tfloor:.2f}: confidence {conf:.2f}",
            confidence=conf,
            payload={"confidence_floor_skipped": f"tier={tier} floor={tfloor}", "feed": "scout"},
        )
    for sym in written_tickers:
        c = by[sym]
        rec.add(
            JournalPersona.SCOUT,
            Stage.CANDIDATE,
            sym,
            Choice.SELECTED,
            ReasonCode.SCOUT_CANDIDATE,
            reason_text=c.thesis,
            confidence=c.confidence,
            payload={"feed": "scout", "origins": list(c.origins), "horizon": c.horizon},
        )
    return len(rec.records)


def _scout_note(ctx: JobContext, out: ScoutReadPayload, session: str, about: list[str]) -> None:
    """One ``regime_view`` note with the Scout's read (D27), for Research to read back.

    E13.13 (note v5): the read's sections verbatim (Regime · Options sentiment · Themes
    · Risks) plus a ``discovery`` fact; no body.
    """
    from pydantic import ValidationError

    from arc.context.kinds import NOTE_SECTION_MAX, NotePayload, NoteSection, NoteTopic

    pairs = [
        ("Regime", out.regime),
        ("Options sentiment", out.options_sentiment),
        ("Themes", "\n".join(f"- {t}" for t in out.themes)),
        ("Risks", "\n".join(f"- {r}" for r in out.risks)),
    ]
    sections = [
        NoteSection(label=label, text=text.strip()[:NOTE_SECTION_MAX])  # type: ignore[arg-type]
        for label, text in pairs
        if text and text.strip()
    ]
    facts: dict[str, str | float | int | bool] = {"discovery": int(out.discovery_fill)}
    try:
        payload = NotePayload(
            persona="scout",
            topic=NoteTopic.REGIME_VIEW,
            title=f"Scout daily read {session}",
            sections=sections,
            facts=facts,
            about=about,
        )
    except ValidationError as exc:
        log.warning("pipeline.note_invalid", persona="scout", error=str(exc))
        return
    ctx.write("note", "market", payload)


BUILTIN_HANDLERS: Mapping[str, str] = {
    "rss": "arc.routines.handlers:rss_source",
    "edgar": "arc.routines.handlers:edgar_source",
    "ticker_news": "arc.routines.handlers:ticker_news_source",  # E14.1 (D60)
    "earnings": "arc.routines.handlers:earnings_source",
    "symbols": "arc.routines.handlers:symbols_source",  # E5.7 weekly symbol master
    "universe.momentum": "arc.routines.handlers:universe_momentum_source",  # E12.2 monthly
    # E13.19 (D58): daily Reddit + Stocktwits pull, then the code-ranked trending tier
    "retail_buzz": "arc.routines.handlers:retail_buzz_source",
    "universe.trending": "arc.routines.handlers:universe_trending_source",
    "youtube": "arc.routines.handlers:youtube_source",
    "youtube.briefs": "arc.routines.handlers:youtube_briefs",  # E4.6 daily briefs (D45)
    # E4.5 / D30 options-trading data (no LLM; typed context kinds)
    "vol_term": "arc.routines.handlers:vol_term_source",
    "options_daily": "arc.routines.handlers:options_daily_source",  # E13.5 (D56)
    "vix_futures": "arc.routines.handlers:vix_futures_source",
    "options_fast": "arc.routines.handlers:options_fast_source",  # E13.6 (D56)
    "macro_calendar": "arc.routines.handlers:macro_calendar_source",
    "ex_dividend": "arc.routines.handlers:ex_dividend_source",
    "iv.record": "arc.routines.handlers:iv_record_source",  # E4.12 (D55) daily 30-DTE IV
    # E4.8 (D46): Finnhub per-ticker context (typed kinds, shared 55/min budget)
    "finnhub.insider": "arc.routines.handlers:finnhub_insider_source",
    "finnhub.recs": "arc.routines.handlers:finnhub_recs_source",
    "finnhub.fundamentals": "arc.routines.handlers:finnhub_fundamentals_source",
    "finnhub.earnings_history": "arc.routines.handlers:finnhub_earnings_history_source",
    "scalp": "arc.routines.handlers:scalp_persona",
    # E13.7 (D56): the daily Scout (YouTube + options_slow)
    "scout": "arc.routines.handlers:scout_persona",
    # E5.2 pipeline chain: research → quant.open → risk.open → [quant.revise] →
    # quant.propose (arc/pipeline/steps.py; E13.9 / D56 step names)
    "research": "arc.pipeline.steps:research_step",
    "quant.open": "arc.pipeline.steps:quant_open_step",
    "risk.open": "arc.pipeline.steps:risk_open_step",
    "quant.revise": "arc.pipeline.steps:quant_revise_step",
    "quant.propose": "arc.pipeline.steps:quant_propose_step",
    # E13.17 (D56): exit cases judged by Quant
    "quant.exit": "arc.pipeline.steps:quant_exit_step",
    # E13.18 (D56): Risk's close | hold review of the exit cases
    "risk.exit": "arc.pipeline.steps:risk_exit_step",
    # E5.3 intraday monitor (read-only: positions, Greeks, expiries, daily-loss halt)
    "monitor": "arc.routines.monitor:monitor_step",
    # E6.2 / D56 Broker: works an approved proposal through its D24 price band
    "broker": "arc.broker.ladder_job:broker_step",
    # D34 in-chain Execute: publish + auto-approve this chain's proposals, hand each
    # to a Broker subprocess (own lock, not the LLM lock); a no-op when auto is off
    "broker.execute": "arc.broker.ladder_job:execute_step",
    # E6.3 / D56 Broker reconcile: post-market broker vs local, snapshots, tax lots, card
    "broker.reconcile": "arc.broker.reconcile_job:broker_reconcile_step",
    # E6.4 position manager: marks -> mandatory exits (arc/positions/steps.py)
    "positions.evaluate": "arc.positions.steps:evaluate_step",
    # E13.18 (D56): the deterministic mandatory-exit floor (stop / DTE exit / expiry)
    "exits.mandatory": "arc.positions.steps:exits_mandatory_step",
    # E7.3 weekly paper scorecard (deterministic, from the audit store; posts as [Ops])
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
