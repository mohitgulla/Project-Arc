# Architecture — Project Arc

## System DAG

```
 Market Data & Math Engine [Deterministic]   Information Retrieval [Sweep persona]
        |  chains, IV, history                      |  RSS, EDGAR, earnings cal, YouTube
        v                                           v
 Greek Analysis [Deterministic]   ------>  Aggregator [Director persona]
   D G v Th rho Vanna Volga, payoff            candidate objects + regime features
                                                    |
                        +---------------------------+-----------------------------+
                        v                                                         v
        Risk/Reward Analysis [Quant persona]                     Portfolio Alignment [Risk persona]
        structure selection, PoP, EV, cost-aware                 concentration, Greek budget, calendar
                        +---------------------------+-----------------------------+
                                                    v
                          +------ RISK PROXY GATE [Deterministic Python, non-bypassable] --------+
                          | 5% per-underlying, 3% daily loss halt, spread/tick, wash-sale 30d    |
                          | portfolio D/v caps, defined-risk whitelist, earnings blackout, halt   |
                          +-----------------------------+----------------------------------------+
                                                        v
                              Decision Processor [Human -- Slack Approve/Reject]
                                                        v
                              Trade Execution [Broker job (deterministic) -> BrokerAdapter.alpaca_paper]
                                                        v
                              Broker reconcile, SQLite audit, reconciliation --> back to Aggregator
```

## Hard boundaries

1. **Personas never call the broker.** Only `arc.execution.submit()` may submit an
   order and it requires a `GateToken` (HMAC over the exact order payload, minted
   by the gate) and an `ApprovalRecord` for the same payload hash.

2. **Hermes `pre_tool_call` hook (fail-closed)** blocks any tool whose name matches
   the broker order tools unless the token check passes.

3. **The gate has no LLM inputs.** Pure functions, 100% branch coverage required.

4. **`ARC_ENV=paper` is the default.** `live` requires a separate credential file
   that does not exist in Phase 1.

5. **Agents share state only through the context store** (`arc/context/`, D16).
   Writes are append-only (DB triggers). Every persona run records the snapshot id it
   read. The gate never reads the store.

## Scheduling: routine dispatcher + context store (E5.4, D16)

```
 Hermes cron (*/10) --> arc routines tick
                             |  config/routines.yaml (sources, personas, chains, triggers)
                             v
   expire TTLs -> plan due slots (cursor..now, catch-up once within TTL, skip if halted)
     -> sources (fetch-only)  --append-->  context_entries (raw_doc_ref, channel_brief)
     -> lane: background jobs (D39: sweep, edgar, ...) -> claim slot, spawn `arc routines run-claimed`
     -> personas [llm lock only for local models, D39]
          snapshot(as_of, kinds) --id--> routine_runs.inputs_snapshot
          handler(ctx) --append--> context_entries (candidate, shortlist, ...)
          chain: director -> quant -> risk -> propose  (one chain_run_id)
     -> triggers: <job>.completed [if cond] -> run job; queued events (approval, halt)
     -> heartbeat line in #arc-investor day thread (sources quiet, failures alert)
```

- `routine_runs` is unique on `(job, scheduled_for)`, so a doubled tick cannot re-run a job.
- A failed chain step stops the chain. Retrying it resumes from that step.
- Adding a source or persona is a YAML edit. Built-in handlers resolve by job-name
  prefix (`youtube.<channel>` uses the YouTube handler), and `handler: module:fn`
  plugs in anything else.

### Agent I/O contracts (E5.6, D27)

Every job and chain step declares `reads` (kinds in its input snapshot) and `writes`
(kinds it may append) in `config/routines.yaml`. `JobContext.write` raises
`ContractViolationError` for an undeclared kind, and the run fails and alerts
(fail-closed). No `writes` key means the job may write nothing. A test requires every
shipped job and step to declare `writes`.

| Job / step | reads | writes |
|---|---|---|
| `rss`, `edgar`, `earnings` | - | `raw_doc_ref` |
| `youtube.briefs` (E4.6, D45) | - | `raw_doc_ref`, `channel_brief` (TTL 48 h from the video's publish time (D60), supersede latest, one per channel) |
| `sweep` | all | `candidate`, `note` (one scan-summary observation per run) |
| `director` | `candidate`, `regime`, `channel_brief`, `note` | `shortlist`, `regime`, `note` |
| `quant` | `shortlist`, `regime` | `structures`, `note` |
| `risk` | `structures` | `risk_review`, `note` |
| `propose` | `candidate`, `regime`, `shortlist`, `structures`, `risk_review` | `proposal` |
| `monitor` | `proposal` | `proposal` (E6.2 exit proposals) |
| `broker.reconcile` | all | `journal`, `note` |
| `scorecard` | audit store (read-only) | nothing (writes the `docs/RESEARCH/weekly/<monday>.md` report, E7.3) |
| `broker` | all | `note` |

- **`note` kind.** `NotePayload` holds persona, topic (`thesis`, `regime_view`,
  `observation`, `risk_flag`, `lesson`, `execution`), horizon, stance, title, body,
  confidence, tags, evidence and `about` (the ids of the entries it annotates). Notes
  keep persona narrative that used to be dropped. The Director reads prior notes,
  newest first and capped by `pipeline_max_context_notes`. Notes are never gate or
  `Candidate` inputs.
- **Run manifest.** The dispatcher writes one append-only `run_manifests` row per run
  attempt, whatever the status (`ok`, `failed` or `skipped`). It is a versioned
  `arc.routines.manifest.RunManifest` covering:
  - identity, trigger and parent run
  - timing and market session
  - outcome and error class
  - env flags (never secret values), git sha and hashes of every `config/` file
  - the effective spec, the declared contract and kind schema versions
  - the input snapshot and its digest
  - external inputs recorded by `JobContext.record_input` (`as_of` + sha256)
  - outputs by kind
  - LLM models, tokens and cost
  - linked decisions, proposals, gate decisions and Slack ts

  A later run-level fact is added as a manifest field, not as a side column.
- **Schema registry.** Committed payload schemas live in
  `schemas/context/<kind>.v<N>.json`. `arc context schemas --check` fails on drift;
  regenerate with `--write` and bump `schema_version` on a change.
- **Trace.** `arc context trace <run_id|chain_run_id> [--json]` shows each step's
  declared contract, the entries it read and wrote, its persona calls and its manifest.

### Daily schedule and cron (E5.3)

Shipped defaults in `config/routines.yaml`, all times ET:

| When | Job |
|---|---|
| 06:00, 18:00 trading days | `earnings` calendar |
| 06:00-20:00 trading days | `rss` every 30m, `edgar` every 15m |
| 09:30 trading days | pre-market chain `director -> quant -> risk -> propose` |
| 09:30-16:00 trading days, every 30m | `monitor`: positions, net Greeks, expiries, daily-loss halt. Read-only, no LLM, halt-exempt |
| 02:00 trading days | `youtube.briefs`: newest video per channel from the last 48 h (D60; 7 channels: 2 youtube_macro, 5 youtube_micro) → one `channel_brief` each, read by the Director (D45). YouTube docs never reach the Sweep |
| 22:00 daily | `sweep.overnight` (fast sources only) |
| 16:30 trading days | `broker.reconcile` |
| 16:45 Fridays (`days: [fri]`) | `scorecard` (weekly) |

- One Hermes cron job, `arc-routines-tick`, runs `*/10 * * * *` (on the clock, D52) with `--no-agent`. It runs
  `hermes/routines/arc_routines_tick.py`; install it with `hermes/routines/install.sh`.
  Success prints nothing. If the tick crashes or times out, Hermes alerts #project-arc.
- Heartbeats post to the session's `💡 Thu Oct 1 · Session Notes` thread in #arc-investor.
  On a trading day every post until `heartbeat.day_rollover` (24:00 ET, i.e. midnight) goes
  to that day's thread, so the 22:00 Sweep run stays in the same day. Weekend/holiday posts
  go to the next session's thread: a Sunday 22:00 run posts in Monday's. Quiet jobs, sources
  and `monitor`, fold into the next persona line, one entry per job. `JobResult.notice`
  posts immediately (halt, expiring positions) as one line, `:warning: `[Routines] …``.
  The thread's first reply is the auto-approve banner; a flip notice that only repeats the
  state already shown in the thread is not posted.
- The `youtube.briefs` summary is `briefs n/7 · StockedUp ✓ FX Evolution – (no video 48h) …`
  followed by the run's caption outcome; a channel listing/LLM failure posts a notice.
- YouTube summaries show the run's caption outcome (ok, rate_limited, empty, error,
  skipped by breaker/cooldown), audio fallbacks with wall time, and the current
  captions cooldown.
- `arc routines tick --dry-run --step 5m --since ... --now ...` simulates each cron tick
  over a window.

## Repo layout

```
Project-Arc/
+-- AGENTS.md
+-- docs/PLAN.md
+-- docs/ARCHITECTURE.md       (this file)
+-- pyproject.toml              uv, Python 3.12, ruff, pytest
+-- Makefile                    make check / make test-gate
+-- .github/workflows/ci.yml   CI on PR
+-- arc/
|   +-- __init__.py
|   +-- cli.py                  arc scan|history|routines|context (more per card)
|   +-- config.py               pydantic-settings; ARC_ENV; limits; universe
|   +-- calendar.py             exchange_calendars: sessions, early closes, DTE
|   +-- models.py               pydantic v2 data contracts (see below)
|   +-- store/                  SQLite schema + migrations + repositories
|   +-- data/                   MarketDataProvider protocol; alpaca, thetadata
|   +-- pricing/                BS + Greeks (py_vollib, QuantLib cross-check)
|   +-- structures/             legs -> payoff, max gain/loss, breakevens, net Greeks
|   +-- scanner/                chain filters, IVR, delta-targeted strikes
|   +-- ingest/                 RSS, EDGAR, earnings, YouTube -> Candidate
|   +-- context/                shared context store: typed kinds, TTLs, snapshots (D16)
|   +-- routines/               config-driven dispatcher: schedule, chains, triggers, locks,
|                               run manifests (D27)
|   +-- features/               regime (Markov 3-state), IV/HV, IVR
|   +-- personas/               JSON schemas + prompt builders (no side effects)
|   +-- gate/                   rules.py (pure), token.py, halt.py
|   +-- approvals/              Slack proposal card, TTL, ApprovalRecord
|   +-- execution/              order state machine, submit(), fills, exits
|   +-- broker/                 BrokerAdapter protocol; alpaca_paper.py
|   +-- reconcile/              broker vs local, PnL snapshots, alerts
|   +-- backtest/               cost-aware engine, walk-forward, reports
+-- schemas/context/            <kind>.v<N>.json: committed payload JSON Schemas (D27)
+-- tests/
+-- hermes/
    +-- skills/arc-*/SKILL.md
    +-- hooks/arc-gate/
    +-- routines/
```

## Data contracts (pydantic v2)

All models live in `arc/models.py`. See PLAN.md section 2.3 for full specifications.

### Candidate
Surfaced by the Sweep persona. Fields: ticker, stance, catalyst_type (enum),
catalyst_date, confidence (0-1), sources[], created_at.

### Structure
A multi-leg option structure. Fields: legs[] (occ_symbol, side, ratio, intent),
net_debit_credit, max_gain, max_loss, breakevens[], greeks (D G v Th rho Vanna Volga),
dte, liquidity (spread_pct, open_interest, volume).

### Proposal
A complete trade proposal. Fields: candidate_id, structure, thesis, quant (pop, ev,
cost_bps), risk_narrative, sizing (contracts, notional, pct_equity), expires_at.

### GateDecision
Output of the deterministic gate. Fields: proposal_hash, passed, violations[], token
(HMAC if passed), account_snapshot.

### ApprovalRecord
Human decision from Slack. Fields: proposal_hash, slack_user, slack_ts, decision
(approved|rejected|expired), at.

### Order
Event-sourced state machine: proposed -> gated -> approved -> submitted ->
partially_filled -> filled | cancelled | rejected | expired. Every transition is
an OrderEvent row.

**Unknown submit state (E11.1, D71).** Every Alpaca HTTP call carries an explicit
`(connect, read)` timeout (`arc.broker.http.TimeoutSession`, 5 s / 15 s). When
`submit()` raises anything but `SubmitRefused`, the ladder never assumes: it looks
the attempt up by its deterministic `client_order_id`
(`arc.execution.resolve.resolve_unknown_submit`). Found → the broker id is
adopted and the attempt is worked normally; absent after a transport error → the
row is cancelled (`order:submit_failed`) and the next band step goes out with its
own id; an API 4xx it does not hold → rejected; lookups failing → one
`cancel_by_client_id`, `unconfirmed`, and one `reconcile.intraday` event for the
proposal. That event-driven job reconciles only the proposal's orders/executions
(`reconcile(scope="intraday")`) and halts + alerts only if the order is still
unresolved. A client id is never submitted twice.

**Ladder liveness and re-attach (E11.2, D72).** A Broker ladder runs in a detached
process (D34); if that process dies after a submit, its DAY order would keep
working with nobody to cancel it. Each run therefore records its `pid` and a
`heartbeat_at` (beaten at each attempt and every poll) on its `routine_runs` row,
and an event-triggered run holds the owner flock `run-<run id>` for its whole
life (the kernel releases it on death). The deterministic `broker.reattach` job
(`arc.execution.reattach`, every tick in RTH, halt-exempt, never submits) treats a
`working` execution as orphaned when that lock is free or the heartbeat is older
than `execution.reattach_stale_s`. It fences the execution
(`executions.adopted_by_run_id`; the old ladder checks the fence before every
send/write and stops with `ExecutionAdoptedError`), resolves each open order at
the broker (by id, else by `client_order_id`), records fills through the ladder's
own `arc.execution.fills.apply_fill`, cancels and confirms the remainder, fails the
dead run and posts one notice per adoption. A ladder that holds its lock but has
stopped beating is *wedged*: alerted, then sent one SIGTERM after
`execution.reattach_kill_after_s` and adopted on a later tick. A unique index on
`fills(order_id, broker_fill_id)` is the last guard against a double fill.
