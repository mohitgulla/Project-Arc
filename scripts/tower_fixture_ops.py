"""E8.7d Ops & pipeline fixture rows (loaded by ``scripts/tower_fixture_db.py --ops``).

A full simulated schedule, anchored on *now*: the routine dispatcher's own dry-run plan
(``arc routines tick --dry-run --step 5m``, the same :meth:`Dispatcher.tick` call) for
yesterday 00:00 ET → *now* is persisted as ``routine_runs`` (sources, persona chains,
monitor, auditor, the 5-min Research loop), each with a D27 run manifest. On top:

- outcomes: the loop's root runs alternate full / ``no_change`` (the D31 digest skip:
  the chain's LLM steps are recorded as skipped); one failed ``edgar`` run; one skipped
  Scalp slot (halted); the last ``monitor`` slot still ``running``;
- a Research run whose manifest writes a ``proposal`` entry it never declared (the
  contract-mismatch highlight), and snapshots of what the runs read;
- context entries of every registered kind (active, and some expired in the last 24 h);
- persona calls with tokens / latency / cost for every full loop, Scalp batches, and
  30 days of history for the LLM trend;
- ops alerts in the 7-day window (with the base fixture's: 1 open, 4 resolved) and
  one older resolved one, a cleared halt from yesterday, a health heartbeat
  with every check, D26 config changes (one applied, one reverted), a YouTube caption
  429 cooldown and a JSON log file with lines for one run.

Opt-in (``--ops``) so the E8.7a-c fixture counts stay unchanged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import TYPE_CHECKING, Any

from arc.context.kinds import KINDS
from arc.context.ttl import to_db
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

GIT_SHA = "0123456789abcdef0123456789abcdef01234567"
UNDECLARED_RUN_TAG = "undeclared"
STEP = dt.timedelta(minutes=5)

#: E8.8e: a universe change of a 20-item list to a 100-item list (DIA, XLF dropped), for the
#: Effective Config / Change Log wrapping checks (>= 100 tickers on one key).
UNIVERSE_OLD: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "XLF", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "AMD", "AVGO", "JPM", "XOM", "UNH", "COST", "NFLX", "CRM",
)  # fmt: skip
UNIVERSE_NEW: tuple[str, ...] = tuple(t for t in UNIVERSE_OLD if t not in ("DIA", "XLF")) + (
    "SMH", "SCHD", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
    "XLRE", "XLC", "GLD", "SLV", "TLT", "HYG", "EEM", "EFA", "FXI", "KWEB",
    "ARKK", "SOXX", "IBB", "XBI", "KRE", "ORCL", "ADBE", "INTC", "QCOM", "TXN",
    "MU", "AMAT", "LRCX", "KLAC", "MRVL", "SNOW", "PLTR", "SHOP", "UBER", "ABNB",
    "PYPL", "SQ", "COIN", "HOOD", "BAC", "WFC", "C", "GS", "MS", "SCHW",
    "V", "MA", "AXP", "BRK.B", "JNJ", "PFE", "MRK", "ABBV", "LLY", "TMO",
    "CVX", "COP", "SLB", "OXY", "HD", "LOW", "WMT", "TGT", "NKE", "SBUX",
    "MCD", "DIS", "CMCSA", "T", "VZ", "BA", "CAT", "DE", "GE", "HON",
    "LMT", "RTX",
)  # fmt: skip

#: Per-model prices used for the fixture costs ($ per 1M input / output tokens).
_PRICE = {"claude-sonnet-5": (3.0, 15.0), "claude-haiku-5": (0.8, 4.0)}
_PERSONA_MODEL = {
    "research": "claude-sonnet-5",
    "quant": "claude-sonnet-5",
    "risk": "claude-sonnet-5",
    "scalp": "claude-sonnet-5",
    "scalp.digest": "claude-haiku-5",
    "risk.reallocate": "claude-haiku-5",
}


def _ins(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ",".join(row)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",  # noqa: S608
        list(row.values()),
    )


def _sha(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def _cost(model: str, tin: int, tout: int) -> float:
    pin, pout = _PRICE[model]
    return round(tin * pin / 1e6 + tout * pout / 1e6, 6)


def plan(routines: Any, since: dt.datetime, now: dt.datetime) -> list[Any]:
    """Every :class:`Outcome` the dispatcher's dry run plans for ticks every 5 min."""
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.heartbeat import LogNotifier
    from arc.routines.locks import NullLocks
    from arc.store.db import connect
    from arc.store.migrate import migrate

    mem = connect(":memory:")
    migrate(mem)
    disp = Dispatcher(mem, routines, locks=NullLocks(), notifier=LogNotifier())
    out: list[Any] = []
    prev, cur = since, since + STEP
    while cur <= now:
        out.extend(disp.tick(cur, dry_run=True, since=prev).outcomes)
        prev, cur = cur, cur + STEP
    mem.close()
    return out


class _Ops:
    def __init__(self, conn: sqlite3.Connection, now: dt.datetime, routines: Any) -> None:
        self.conn = conn
        self.now = now
        self.routines = routines
        self.loop_job = routines.loop.job
        self.entries: dict[str, list[str]] = {}
        self.run_ids: dict[str, str] = {}

    # -- context -----------------------------------------------------------------

    def entry(  # noqa: PLR0913 - one argument per column
        self,
        eid: str,
        kind: str,
        subject: str,
        *,
        at: dt.datetime,
        ttl: dt.timedelta | None,
        by: str,
        run_id: str | None = None,
        chain: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        _ins(self.conn, "context_entries", {
            "id": eid, "kind": kind, "subject": subject,
            "payload": json.dumps(payload or {"schema_version": KINDS[kind].schema_version,
                                              "fixture": True, "subject": subject}),
            "schema_version": KINDS[kind].schema_version, "produced_by": by,
            "run_id": run_id, "chain_run_id": chain, "created_at": to_db(at),
            "valid_from": to_db(at), "expires_at": to_db(at + ttl) if ttl else None,
            "status": "active",
        })  # fmt: skip
        self.entries.setdefault(kind, []).append(eid)
        return eid

    def snapshot(self, sid: str, at: dt.datetime, ids: list[str], run_id: str) -> str:
        _ins(self.conn, "context_snapshots", {
            "id": sid, "as_of": to_db(at), "kinds": "[]", "subjects": "[]",
            "entry_ids": json.dumps(ids), "run_id": run_id, "created_at": to_db(at),
        })  # fmt: skip
        return sid

    # -- runs --------------------------------------------------------------------

    def run(  # noqa: PLR0913 - one argument per column
        self,
        o: Any,
        *,
        status: str,
        summary: str | None,
        error: str | None = None,
        started: dt.datetime,
        finished: dt.datetime | None,
        chain: str | None,
        snapshot_ids: list[str] | None = None,
        outputs: list[str] | None = None,
    ) -> str:
        rid = f"run-ops-{o.job.replace('.', '-')}-{o.scheduled_for:%m%d%H%M}"
        _ins(self.conn, "routine_runs", {
            "run_id": rid, "job": o.job, "chain_run_id": chain, "step_index": o.step_index,
            "reason": "schedule" if o.step_index == 0 else o.reason,
            "scheduled_for": to_db(o.scheduled_for), "started_at": to_db(started),
            "finished_at": to_db(finished) if finished else None, "status": status,
            "attempts": 1, "inputs_snapshot": json.dumps(snapshot_ids or []),
            "outputs": json.dumps(outputs or []), "summary": summary, "error": error,
            "config_version": 2,
        })  # fmt: skip
        return rid

    def manifest(  # noqa: PLR0913 - one argument per manifest group
        self,
        rid: str,
        o: Any,
        *,
        status: str,
        summary: str | None,
        error: str | None,
        started: dt.datetime,
        finished: dt.datetime,
        chain: str | None,
        snapshot_ids: list[str],
        outputs: dict[str, list[str]],
        calls: list[dict[str, Any]],
        extra: dict[str, Any] | None = None,
    ) -> None:
        from arc.routines.manifest import ExternalInput, RunManifest
        from arc.utils.calendar import session_phase

        kind, spec = self.routines.step(o.job)
        tin = sum(c["input_tokens"] for c in calls) or None
        tout = sum(c["output_tokens"] for c in calls) or None
        cost = round(sum(c["cost_usd"] for c in calls), 6) or None
        ext = []
        if kind.value == "source":
            ext.append(ExternalInput(name=o.job, source="fixture", as_of=started,
                                     digest=_sha(rid, "ext"), count=3))  # fmt: skip
        if o.job in (self.loop_job, "quant"):
            ext.append(ExternalInput(name="chain:SPY", source="alpaca", as_of=started,
                                     digest=_sha(rid, "chain"), count=412))  # fmt: skip
        m = RunManifest(
            run_id=rid, job=o.job, job_kind=kind.value, chain_run_id=chain,
            step_index=o.step_index, attempt=1,
            reason="schedule" if o.step_index == 0 else o.reason,
            scheduled_for=o.scheduled_for, tick_now=o.scheduled_for, started_at=started,
            finished_at=finished, duration_ms=int((finished - started).total_seconds() * 1000),
            market_session=session_phase(started), trading_day=started.date(),
            status=status, summary=summary, error_class=error.split(":", 1)[0] if error else None,
            error=error, metrics=dict(extra or {}), arc_env="paper",
            account_profile="cash_debit", halted=False, auto_approve=False,
            order_budget={"used": 31, "limit": 200, "tier": "normal"},
            arc_version="0.1.0", git_sha=GIT_SHA, git_dirty=False, python="3.12.9",
            host="fixture", correlation={"tick_id": f"tick-{o.scheduled_for:%H%M}"},
            config_hashes={"routines.yaml": "a1b2c3d4e5f6", "exits.yaml": "e5f6a7b8",
                           "costs.yaml": "c9d0e1f2"},
            config_version="2", effective_spec=spec.model_dump(mode="json"),
            declared_reads=list(spec.reads) if spec.reads is not None else None,
            declared_writes=list(spec.writes) if spec.writes is not None else None,
            kind_schema_versions={k: KINDS[k].schema_version for k in (spec.reads or [])},
            snapshot_ids=snapshot_ids, input_counts={}, input_digest=_sha(rid, "in"),
            external_inputs=ext, output_ids=outputs,
            persona_call_ids=[c["id"] for c in calls],
            models_requested=sorted({c["model"] for c in calls}),
            models_served=sorted({c["model"] for c in calls}),
            prompt_sha256=[c["prompt_sha256"] for c in calls], input_tokens=tin,
            output_tokens=tout, llm_latency_ms=sum(c["latency_ms"] for c in calls) or None,
            cost_usd=cost,
            notifications=["1790000000.000300"] if o.job == self.loop_job and calls else [],
        )  # fmt: skip
        _ins(self.conn, "run_manifests", {
            "id": f"rm-{rid[4:]}", "run_id": rid, "attempt": 1, "job": o.job,
            "chain_run_id": chain, "status": status, "schema_version": m.schema_version,
            "payload": m.model_dump_json(), "created_at": to_db(finished),
        })  # fmt: skip

    def call(self, rid: str, persona: str, at: dt.datetime, n: int) -> dict[str, Any]:
        model = _PERSONA_MODEL.get(persona, "claude-sonnet-5")
        tin, tout = 4000 + 900 * (n % 5), 600 + 70 * (n % 7)
        c = {
            "id": f"pc-{rid[8:]}-{persona}", "run_id": rid, "persona": persona, "model": model,
            "snapshot_id": None, "prompt_sha256": _sha(rid, persona), "raw_response": "{}",
            "status": "ok", "error": None, "dropped": "{}", "created_at": to_db(at),
            "input_tokens": tin, "output_tokens": tout, "latency_ms": 4000 + 150 * (n % 9),
            "cost_usd": _cost(model, tin, tout),
        }  # fmt: skip
        _ins(self.conn, "persona_calls", c)
        return c


def add_ops(conn: sqlite3.Connection, now: dt.datetime) -> None:  # noqa: C901, PLR0912, PLR0915 - one linear scenario
    """Add the Ops page rows to a fixture DB (see the module docstring)."""
    from arc.control.store import ConfigChangeRepo
    from arc.monitoring.store import AlertRepo, HeartbeatRepo
    from arc.routines.config import load_routines

    routines = load_routines()
    ops = _Ops(conn, now, routines)
    today = now.date()
    since = dt.datetime.combine(today - dt.timedelta(days=1), dt.time(0), tzinfo=ET)
    # E13.7: the D54 cutover (migration 022) is stamped at migrate time, after this
    # fixture's clock, so its `scout` runs would read as pre-rename Scalp. Production's
    # cutover (2026-10-06 01:20Z) predates every Scout run: pin it before the window.
    conn.execute(
        "UPDATE routine_state SET value = ? WHERE key = 'rename:scout_to_sweep'",
        (to_db(since - dt.timedelta(days=30)),),
    )
    outcomes = [o for o in plan(routines, since, now) if o.status == "planned"]

    # -- context entries of every kind (the run outputs below add more) ---------------
    base_at = now - dt.timedelta(hours=2)
    for i, kind in enumerate(KINDS):
        ttl = None if kind in ("journal", "raw_doc_ref") else dt.timedelta(hours=2 + i % 5)
        ops.entry(f"ctx-ops-{kind}", kind, "market" if i % 3 else "SPY",
                  at=base_at + dt.timedelta(minutes=3 * i), ttl=ttl, by="fixture")  # fmt: skip
    for i, kind in enumerate(("candidate", "regime", "shortlist")):
        at = now - dt.timedelta(hours=20 - i)
        ops.entry(f"ctx-ops-expired-{kind}", kind, "QQQ", at=at, ttl=dt.timedelta(hours=4),
                  by="fixture")  # fmt: skip

    # -- runs: the dry-run plan with outcomes ----------------------------------------
    chain: str | None = None
    seen: set[tuple[str, str]] = set()
    loop_n = 0
    failed_done = scalp_skipped = undeclared_done = False
    last_monitor = max((o.scheduled_for for o in outcomes if o.job == "monitor"), default=None)
    root_state: dict[str, str] = {}  # chain id -> "full" | "no_change" | "failed"
    call_n = 0
    for o in outcomes:
        root = o.step_index == 0
        slot_key = (o.job, to_db(o.scheduled_for))
        if slot_key in seen:
            # the dispatcher's (job, slot) unique key: a second chain's shared step
            # (``execute``) is a no-op "duplicate" in the same slot, never recorded
            continue
        seen.add(slot_key)
        started = o.scheduled_for + dt.timedelta(seconds=4 + 3 * o.step_index)
        if root:
            # a chain root's steps follow it in the plan (same slot, step_index > 0)
            spec = routines.job(o.job)
            chain = None
            if spec is not None and spec[1].chain:
                chain = f"chain-ops-{o.job.replace('.', '-')}-{o.scheduled_for:%m%d%H%M}"
        status, summary, error = "ok", f"{o.job} ok", None
        dur = dt.timedelta(seconds=6 if o.job == "monitor" else 40)
        snaps: list[str] = []
        outs: list[str] = []
        out_ids: dict[str, list[str]] = {}
        calls: list[dict[str, Any]] = []
        extra: dict[str, Any] = {}
        rid_guess = f"run-ops-{o.job.replace('.', '-')}-{o.scheduled_for:%m%d%H%M}"
        if o.job == ops.loop_job and root:
            loop_n += 1
            full = loop_n % 6 == 1
            root_state[chain or ""] = "full" if full else "no_change"
            if full:
                summary = "shortlist 1: SPY bull call debit (regime bull, IV rank 0.34)"
                snaps = [
                    ops.snapshot(
                        f"snap-{rid_guess[8:]}",
                        started,
                        [ops.entries["candidate"][0], ops.entries["regime"][0]],
                        rid_guess,
                    )
                ]
                call_n += 1
                calls.append(ops.call(rid_guess, "research", started + dt.timedelta(seconds=20),
                                      call_n))  # fmt: skip
                sl = ops.entry(f"ctx-{rid_guess[8:]}-shortlist", "shortlist", "market",
                               at=started + dt.timedelta(seconds=30),
                               ttl=dt.timedelta(minutes=30), by=o.job, run_id=rid_guess,
                               chain=chain)  # fmt: skip
                outs.append(sl)
                out_ids["shortlist"] = [sl]
                if not undeclared_done and o.scheduled_for.date() == today:
                    # contract mismatch: a proposal entry Research never declared
                    bad = ops.entry(f"ctx-{rid_guess[8:]}-{UNDECLARED_RUN_TAG}", "proposal",
                                    "SPY", at=started + dt.timedelta(seconds=31),
                                    ttl=dt.timedelta(minutes=30), by=o.job, run_id=rid_guess,
                                    chain=chain)  # fmt: skip
                    outs.append(bad)
                    out_ids["proposal"] = [bad]
                    undeclared_done = True
                dur = dt.timedelta(seconds=95)
            else:
                summary = "no_change: inputs unchanged since the last full loop (25m ago)"
                extra = {"no_change": True, "shortlist": 0, "loop_digest": _sha(loop_n)}
                dur = dt.timedelta(seconds=3)
        elif not root and root_state.get(chain or "") == "no_change":
            if o.job != "execute":
                status = "skipped"
                summary = "no_change: inputs unchanged since the last full loop"
                dur = dt.timedelta(0)
            else:
                summary = "execute: 0 approved proposals"
        elif not root and o.job in ("quant", "risk", "risk.reallocate"):
            call_n += 1
            calls.append(ops.call(rid_guess, o.job, started + dt.timedelta(seconds=15), call_n))
            dur = dt.timedelta(seconds=60)
        elif o.job == "edgar" and not failed_done and o.scheduled_for.date() == today:
            status, summary = "failed", None
            error = "HTTPError: 503 Service Unavailable (sec.gov)"
            failed_done = True
        elif o.job == "scalp" and not scalp_skipped and o.scheduled_for.date() == today:
            status, summary = "skipped", "halted: trading halt active (persona jobs skip)"
            scalp_skipped = True
            dur = dt.timedelta(0)
        elif o.job in ("scalp", "scalp.overnight"):
            call_n += 1
            calls.append(ops.call(rid_guess, "scalp", started + dt.timedelta(seconds=25), call_n))
            cand = ops.entry(f"ctx-{rid_guess[8:]}-candidate", "candidate", "SPY",
                             at=started + dt.timedelta(seconds=40), ttl=dt.timedelta(hours=6),
                             by=o.job, run_id=rid_guess)  # fmt: skip
            outs.append(cand)
            out_ids["candidate"] = [cand]
            summary = "1 candidate (SPY) from 12 docs across 5 sources"
        elif o.job == "youtube.briefs":
            # E8.8d: per-channel brief outcomes (E4.6 manifest metrics) for the Sources card
            from arc.ingest.sources import SourceRegistry

            slugs = [s.key.split(".", 1)[1] for s in SourceRegistry.from_routines(routines).sources.values()
                     if s.job == o.job and s.channel]  # fmt: skip
            shapes = [
                {"outcome": "briefed", "title": "Fed week ahead"},
                {"outcome": "pending", "pending_reason": "no captions yet"},
                {"outcome": "no_video"},
            ]
            extra = {"channels": {c: shapes[i % 3] for i, c in enumerate(slugs)}}
            summary = f"{len(slugs)} channels: 1 brief, 1 pending, 1 no video"
        running = (
            o.job == "monitor"
            and o.scheduled_for == last_monitor
            and (now - o.scheduled_for < STEP)
        )
        if running:
            status, summary = "running", None
        finished = None if running else started + dur
        rid = ops.run(o, status=status, summary=summary, error=error, started=started,
                      finished=finished, chain=chain, snapshot_ids=snaps, outputs=outs)  # fmt: skip
        assert rid == rid_guess
        if not running:
            assert finished is not None
            ops.manifest(rid, o, status=status, summary=summary, error=error, started=started,
                         finished=finished, chain=chain, snapshot_ids=snaps, outputs=out_ids,
                         calls=calls, extra=extra)  # fmt: skip

    # -- LLM history: 30 days, a few calls a day, and Scalp batches ------------------
    for d in range(1, 31):
        day_at = dt.datetime.combine(today - dt.timedelta(days=d), dt.time(11, 0), tzinfo=ET)
        for j, persona in enumerate(("research", "quant", "risk")):
            for k in range(1 + (d + j) % 3):
                call_n += 1
                ops.call(f"run-ops-hist-{d:02d}-{j}{k}", persona,
                         day_at + dt.timedelta(minutes=10 * k + j), call_n)  # fmt: skip
    for d in range(0, 8):
        at = now - dt.timedelta(days=d, hours=1)
        for stage, model in (("digest", "claude-haiku-5"), ("scalp", "claude-sonnet-5")):
            tin, tout = 9000 + 500 * d, 1200
            _ins(conn, "scalp_batches", {
                "id": f"sb-ops-{d}-{stage}", "run_id": f"run-ops-sb-{d}", "model": model,
                "doc_ids": "[]", "prompt_sha256": _sha(d, stage), "raw_response": "{}",
                "status": "ok", "error": None, "accepted": 1, "rejected": "{}",
                "created_at": to_db(at), "stage": stage, "input_tokens": tin,
                "output_tokens": tout, "cost_usd": _cost(model, tin, tout),
            })  # fmt: skip

    # -- raw docs per source (today + yesterday), one budget skip ---------------------
    from arc.ingest.sources import SourceRegistry

    reg = SourceRegistry.from_routines(routines)
    for i, s in enumerate(reg.sources.values()):
        for k in range(1 + i % 3):
            at = now - dt.timedelta(hours=1 + k * 3 + i % 4)
            _ins(conn, "raw_docs", {
                "id": f"doc-ops-{s.key}-{k}", "source": s.job.split(".", 1)[0],
                "url": f"https://example.test/{s.key}/{k}", "published_at": to_db(at),
                "text": "fixture", "tickers_hint": '["SPY"]',
                "content_hash": _sha("doc", s.key, k), "ingested_at": to_db(at),
                "source_key": s.key,
                "scalp_status": "skipped_budget" if (i, k) == (1, 1)
                else "skipped_stale" if (i, k) == (0, 0) else "scouted",
            })  # fmt: skip
    _ins(conn, "ingest_cursors", {
        "connector": "youtube:captions_backoff",
        "cursor_val": json.dumps({"cooldown_until": (now + dt.timedelta(hours=2)).isoformat(),
                                  "consecutive_rate_limits": 2}),
        "updated_at": to_db(now - dt.timedelta(minutes=30)),
    })  # fmt: skip

    # -- alerts (3 in the window, 1 open), a cleared halt, health checks --------------
    alerts = AlertRepo(conn)
    # (the base fixture's open ``missed:scalp`` alert is the one open alert)
    alerts.open("stuck:monitor", "stuck_run", "monitor run still running after 10 min",
                at=now - dt.timedelta(minutes=45))  # fmt: skip
    alerts.resolve("stuck:monitor", at=now - dt.timedelta(minutes=31))
    edgar_slot = next((o.scheduled_for for o in outcomes if o.job == "edgar"), now)
    alerts.open(f"missed:edgar:{to_db(edgar_slot)}", "missed_window",
                f"edgar slot {edgar_slot:%H:%M} ET missed", at=now - dt.timedelta(hours=3),
                resolved=True)  # fmt: skip
    for k in range(1, 4):  # E8.8d: repeats collapse into one `missed_window ×n` row
        alerts.open(f"missed:rss:{k}", "missed_window", f"rss slot -{k}h missed",
                    at=now - dt.timedelta(hours=3 + k), resolved=True)  # fmt: skip
    alerts.open("tick_stale", "tick_stale", "no tick heartbeat for 17 min",
                at=now - dt.timedelta(days=1, hours=2))  # fmt: skip
    alerts.resolve("tick_stale", at=now - dt.timedelta(days=1, hours=1, minutes=40))
    alerts.open("remote_access", "remote_access_down", "tower port not answering (old)",
                at=now - dt.timedelta(days=12))  # fmt: skip
    alerts.resolve("remote_access", at=now - dt.timedelta(days=12) + dt.timedelta(minutes=20))
    _ins(conn, "halts", {
        "id": "halt-ops-daily-loss", "at": to_db(now - dt.timedelta(days=1, hours=5)),
        "cleared_at": to_db(now - dt.timedelta(days=1, hours=3)), "reason":
        "daily loss 2.1% >= 2.0% of equity", "actor": "arc:monitor", "run_id": None,
        "cleared_by": "owner (Slack)", "kind": "daily_loss",
        "session_date": (today - dt.timedelta(days=1)).isoformat(),
    })  # fmt: skip
    HeartbeatRepo(conn).record("health", "failed", at=now - dt.timedelta(minutes=9), detail={
        "checks": {
            "gateway": {"severity": "ok", "summary": "Gateway is running"},
            "remote_access": {"severity": "ok", "summary": "dashboard 1994 + tower 4174 answer"},
            "routine_windows": {"severity": "failed", "summary": "1 missed (edgar)"},
            "stuck_runs": {"severity": "ok", "summary": "0 stuck"},
            "tick": {"severity": "ok", "summary": "last tick 1 min ago"},
        },
        "opened": [], "resolved": ["stuck:monitor"], "posted_ts": None,
    })  # fmt: skip

    # -- D26 config changes: one applied, one reverted --------------------------------
    changes = ConfigChangeRepo(conn)
    c1 = changes.append(key="max_open_positions", old=5, new=4, is_default=False,
                        actor="U0OWNER", reason="tighten while halted", source="slack",
                        at=now - dt.timedelta(days=2), status="applied",
                        direction="safer")  # fmt: skip
    changes.append(key="scalp_min_confidence", old=0.6, new=0.5, is_default=False,
                   actor="U0OWNER", reason="more candidates", source="slack",
                   at=now - dt.timedelta(days=1, hours=6), status="applied",
                   direction="riskier")  # fmt: skip
    changes.append(key="scalp_min_confidence", old=0.5, new=None, is_default=True,
                   actor="U0OWNER", reason="revert", source="slack",
                   at=now - dt.timedelta(days=1), status="reverted", direction="safer",
                   supersedes_id=c1.id + 1)  # fmt: skip
    # E8.8e: a long list change (20 -> 100 tickers, DIA and XLF dropped) by the owner's real
    # Slack id, so the page shows its `tower.actor_names` name and the wrapping list diff.
    changes.append(key="universe", old=list(UNIVERSE_OLD), new=list(UNIVERSE_NEW),
                   is_default=False, actor="U0C5KUMH28G", reason="widen the seed list",
                   source="cli", at=now - dt.timedelta(hours=20), status="applied",
                   direction="riskier")  # fmt: skip
    add_universe(conn, now)


#: E12.6: the tiered-universe fixture (trending names with E12.3-style reasons).
MOMENTUM_FIXTURE: tuple[str, ...] = (
    "MU", "AAPL", "NVDA", "AVGO", "PLTR", "ORCL", "GE", "LLY", "JPM", "NFLX",
    "META", "GS", "APP", "RTX", "AXP", "CAT", "MS", "WMT", "HWM", "TJX",
    "KKR", "VST", "CEG", "ANET",
)  # fmt: skip
TRENDING_FIXTURE: tuple[str, ...] = (
    "RKLB", "NVDA", "ASTS", "OKLO", "APP", "IONQ", "SOUN", "HIMS",
)  # fmt: skip
DISCOVERY_FIXTURE: tuple[str, ...] = (
    "QCOM", "CRWV", "NBIS", "TEM", "SOFI", "UBER", "BAC", "SMCI", "MARA", "SNDK", "BBAI",
)  # fmt: skip


def add_universe(conn: sqlite3.Connection, now: dt.datetime) -> None:
    """E12.6: a momentum feed (partial, Schwab-style), a trending feed and an
    ``active_universe`` resolve over them via the real :func:`resolve_active`
    (core 25 + momentum 15 + trending 6 + discovery 6 = 52 > 50, so 2 names are
    ``over_active_cap``). The 100-name ``universe`` override above is ignored (> 30)."""
    from arc.config import ArcSettings
    from arc.context.store import ContextStore
    from arc.context.ttl import Ttl
    from arc.universe.tiers import (
        Tier,
        TierMember,
        UniverseTierPayload,
        resolve_active,
        yaml_core,
    )

    today = now.date()
    store = ContextStore(conn)
    m_at = now - dt.timedelta(days=3, hours=2)
    momentum = [
        TierMember(ticker=t, tier=Tier.MOMENTUM, rank=i, source="stockanalysis",
                   reason=f"SPMO weight {9.5 - i * 0.33:.2f}% (row {i})",
                   as_of=today - dt.timedelta(days=4))
        for i, t in enumerate(MOMENTUM_FIXTURE, 1)
    ]  # fmt: skip
    store.write(
        kind="universe_tier", subject="momentum", produced_by="universe.momentum",
        payload=UniverseTierPayload(
            tier=Tier.MOMENTUM, members=momentum, fetched_at=m_at, source="stockanalysis",
            source_as_of=today - dt.timedelta(days=4), digest="fixture",
            url="https://stockanalysis.com/etf/spmo/holdings/", partial=True,
        ),
        ttl=Ttl(duration=dt.timedelta(days=35)), valid_from=m_at, now=m_at,
    )  # fmt: skip
    t_at = now - dt.timedelta(hours=3)
    trending = [
        TierMember(ticker=t, tier=Tier.TRENDING, rank=i, source="news+reddit+stocktwits",
                   reason=f"reddit #{i} (+{40 - 4 * i} 24h), stocktwits, {5 - i % 3} news "
                   f"sources · score {0.9 - 0.05 * i:.2f}", as_of=today)
        for i, t in enumerate(TRENDING_FIXTURE, 1)
    ]  # fmt: skip
    store.write(
        kind="universe_tier", subject="trending", produced_by="universe.trending",
        payload=UniverseTierPayload(
            tier=Tier.TRENDING, members=trending, fetched_at=t_at,
            source="news+reddit+stocktwits", source_as_of=today, digest="fixture",
        ),
        ttl=Ttl(duration=dt.timedelta(hours=20)), valid_from=t_at, now=t_at,
    )  # fmt: skip
    core = [
        TierMember(ticker=t, tier=Tier.CORE, rank=i, source="config", reason="core list",
                   as_of=today)
        for i, t in enumerate(yaml_core(ArcSettings()), 1)
    ]  # fmt: skip
    disc = [
        TierMember(ticker=t, tier=Tier.DISCOVERY, rank=i, source="scalp",
                   reason=f"candidate confidence {0.8 - 0.05 * i:.2f}, corroboration {4 - i}",
                   as_of=today)
        for i, t in enumerate(DISCOVERY_FIXTURE, 1)
    ]  # fmt: skip
    active = resolve_active(
        core=core, momentum=momentum, trending=trending, discoveries=disc, active_max=50,
        tier_sizes={Tier.MOMENTUM: 25, Tier.TRENDING: 25}, as_of=today, config_version=4,
    )  # fmt: skip
    a_at = now - dt.timedelta(minutes=20)
    store.write(
        kind="active_universe", subject="active", produced_by="scalp", payload=active,
        ttl=Ttl(duration=dt.timedelta(hours=20)), valid_from=a_at, now=a_at,
    )  # fmt: skip


def write_log(db_path: Path, run_id: str, now: dt.datetime) -> Path:
    """``logs/arc.jsonl`` next to the DB, with a few lines about *run_id*."""
    path = db_path.resolve().parent / "logs" / "arc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"ts": (now - dt.timedelta(seconds=40)).isoformat(), "level": "info",
         "event": "routines.started", "run_id": run_id, "job": "research"},
        {"ts": (now - dt.timedelta(seconds=30)).isoformat(), "level": "warning",
         "event": "context.undeclared_write", "run_id": run_id, "kind": "proposal"},
        {"ts": (now - dt.timedelta(seconds=10)).isoformat(), "level": "info",
         "event": "routines.finished", "run_id": run_id, "status": "ok"},
        {"ts": now.isoformat(), "level": "info", "event": "routines.tick", "counts": {"ok": 3}},
    ]  # fmt: skip
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def undeclared_run_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM context_entries WHERE id LIKE ?", (f"%-{UNDECLARED_RUN_TAG}",)
    ).fetchone()
    return None if row is None else str(row[0])
