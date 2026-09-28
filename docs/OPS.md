# Project Arc — Operations Guide

**Status:** v0.2 · 2026-09-27 · maintained by: Hermes workers (E8 cards)

---

## 1. LLM Provider Configuration

### 1.1 Primary provider

| Setting | Value |
|---------|-------|
| Provider | Anthropic (subscription) |
| Default model | `anthropic/claude-opus-5.5` |
| Auth | `CLAUDE_CODE_OAUTH_TOKEN` in `~/.hermes/.env` |
| Hermes config | `model.default: anthropic/claude-opus-5.5`, `model.provider: auto` |

Decision D8 (PLAN.md §0): Anthropic Subscription (Claude Opus 5.5) for all
implementation/execution tasks. Both persona tiers (§2) also run on the
subscription.

### 1.2 Fallback provider

TODO: Fallback provider: not configured; wire in later — D8.

---

## 2. Per-Persona Model Tiers

PLAN.md §2.4 / D8 define two model tiers, both on the Anthropic subscription:

| Tier | Personas | Model |
|------|----------|-------|
| **frontier** | Director, Quant, Risk | `anthropic/claude-opus-5.5` |
| **cheap** | Scout, Investor, Auditor | `anthropic/claude-opus-5` |

### 2.1 Where it is configured (one place)

`config/llm_routing.yaml` is the only place a persona's or a tier's model is
set:

```yaml
tiers:
  frontier: {model: anthropic/claude-opus-5.5}
  cheap:    {model: anthropic/claude-opus-5}
personas:
  director: frontier
  quant:    frontier
  risk:     frontier
  scout:    cheap
  investor: cheap
  auditor:  cheap
```

- Change a whole tier: edit its `model`.
- Move one persona: change its tier name under `personas`.
- Model ids are `<provider>/<model>`; the prefix becomes `hermes -z --provider`.
- Use a different file (e.g. for an experiment) with `ARC_LLM_ROUTING_FILE`.

The file is validated by `arc.llm_routing` (pydantic, `extra="forbid"`):
every §2.4 persona must be listed, every tier it names must exist, and every
model must be provider-qualified. `tests/test_llm_routing.py` checks that each
persona resolves to the model above and that the persona SKILL.md
`**Model tier:**` lines agree with the config.

### 2.2 How call sites use it

Personas run as Hermes one-shots (`hermes -z -m <model> --provider <provider>
--ignore-rules -t todo`) via `arc.ingest.llm.HermesScoutLLM`. No call site
names a model:

- Scout (`arc ingest` / `run_scout`): `HermesScoutLLM.from_settings(settings)`
  → persona `scout`.
- Director / Quant / Risk (`arc propose`, `PipelineEnv.live`):
  `HermesScoutLLM.from_settings(settings, persona, timeout_seconds=...)`, one
  backend per persona.
- Investor / Auditor: use `arc.llm_routing.resolve("investor"|"auditor", settings)`
  (or `HermesScoutLLM.from_settings(settings, "<persona>")`) when their
  runners land.

The model that actually answered is read back from the Hermes usage file and
stored with each persona reply for audit.

### 2.3 Cost estimation (per pipeline run)

| Persona | Calls/run | Tokens/call (est.) | Tier |
|---------|-----------|-------------------|------|
| Scout | 1-5 | ~2K in / ~1K out | cheap |
| Director | 1 | ~4K in / ~2K out | frontier |
| Quant | 1-3 | ~4K in / ~3K out | frontier |
| Risk | 1 | ~3K in / ~2K out | frontier |
| Investor | 0-2 | ~1K in / ~500 out | cheap |
| Auditor | 1 | ~2K in / ~1K out | cheap |

All calls draw on the subscription quota; the cheap tier keeps high-volume
personas off the frontier model.

---

## 3. Environment and Secrets

### 3.1 Required secrets (`~/.hermes/.env`)

| Variable | Purpose | Set? |
|----------|---------|------|
| `CLAUDE_CODE_OAUTH_TOKEN` | Anthropic subscription auth | Yes |
| `SLACK_BOT_TOKEN` | Hermes Slack bot | Yes |
| `SLACK_APP_TOKEN` | Slack Socket Mode | Yes |
| `ALPACA_API_KEY` | Alpaca paper trading | Yes |
| `ALPACA_SECRET_KEY` | Alpaca paper trading | Yes |
| `ALPACA_BASE_URL` | Alpaca API endpoint | Yes |

### 3.2 Environment switch

`ARC_ENV` defaults to `paper`. The `live` credential file does not exist.
Never set `ARC_ENV=live` in Phase 1.

---

## 4. Hermes Configuration Summary

### 4.1 Active config (`~/.hermes/config.yaml`)

| Section | Key | Value |
|---------|-----|-------|
| `model.default` | Primary model | `anthropic/claude-opus-5.5` |
| `model.provider` | Provider resolution | `auto` (resolves to Anthropic) |
| `kanban.review_dispatch` | Auto-dispatch reviewers | `true` |
| `delegation.max_iterations` | Subagent turn cap | `250` |

### 4.2 Profiles

Phase 1 uses a single `default` profile. A dedicated `arc-worker` profile is
deferred (PLAN.md §2.6). Per-persona routing is done by Arc itself
(`config/llm_routing.yaml`), not by separate Hermes profiles.

---

## 5. Monitoring (E8.2)

Config: the `monitoring:` section of `config/routines.yaml` (validated by
`arc routines validate`). Code: `arc/monitoring/`. Tables: `heartbeats`
(append-only) and `ops_alerts` (migration 010).

### 5.1 What runs

| Piece | Runs as | Does |
|---|---|---|
| `arc routines tick` | Hermes cron `arc-routines-tick`, every 5m (`hermes/routines/install.sh`) | Records one `tick` heartbeat per live tick with its `tick_id`, outcome counts and run ids. A crash still records a `failed` heartbeat. |
| `arc health check` | launchd agent `com.projectarc.health-check`, every 30m (`hermes/monitoring/install.sh`) | Runs the checks below, records a `health` heartbeat, and opens/resolves ops alerts. Exit 1 while anything is failing. |

The health check runs under launchd rather than as a Hermes cron. The gateway
hosts the cron ticker, so a check running inside it could not report the
gateway (or the ticker) being down.

Install or refresh it with `hermes/monitoring/install.sh [REPO_DIR]`. Preview
the plist with `--print`, and remove it with `--uninstall`.

### 5.2 Checks

| Check | Fails when | Alert key |
|---|---|---|
| tick | There is no `tick` heartbeat for `tick_stale_after` (15m), or none ever. | `tick_stale` |
| routine_windows | A scheduled slot's catch-up window (+`miss_grace` 10m) closed and the slot never ran or was recorded as missed. Slots are only judged after the first tick heartbeat, looking back `miss_lookback` (1d). | `missed:<job>:<slot>` |
| stuck_runs | A `routine_runs` row is still `running` after `stuck_after` (70m). | `stuck:<run_id>` |
| gateway | `hermes gateway status` or `hermes cron status` shows a `✗`, exits non-zero, or times out. `⚠` warnings count as degraded: they are recorded but not alerted unless `gateway.alert_on_degraded: true`. | `gateway` |

Some skips are deliberate and are never counted as misses: halted personas and
jobs with no handler yet. Job failures are already alerted in the #arc-investor
day thread, and those alerts now end with the run id.

### 5.3 Alerts

Alerts are posted to `#project-arc` (`monitoring.alert_channel`) as one message
per check run:

- A condition alert (tick, stuck run, gateway) is posted once when it opens.
- While the condition persists, it is not posted again.
- When the check passes again, a `resolved` line is posted. It describes the
  current state (e.g. "routines tick heartbeat is fresh again (last tick 2 min
  ago, tick-…)"), not the text the alert opened with.
- A missed routine window is posted exactly once. Several missed slots of the
  same job in one post collapse into one line per job.
- Outages don't flood the channel. While a `tick_stale` or `gateway` incident is
  open, or opens in the same run, any missed slot whose window closed during
  it (from the last good tick onward) is recorded with
  `correlation.folded_into = <incident alert id>` and is not posted on its own.
  The incident's resolve line summarises them, e.g. "during it 23 routine
  slot(s) missed: rss ×18, edgar ×4, director ×1". A slot judged after the
  incident resolved goes out as one thread reply under the incident post. A
  full outage therefore produces two root posts: one when it opens and one
  when it resolves.

Each post carries the alert id and the correlation ids. If Slack is
unreachable, the alert is still recorded in the DB with `posted_ts` NULL.

### 5.4 Logs and rotation

| File | Written by | Rotation |
|---|---|---|
| `data/logs/arc.jsonl` | Structured JSON lines from `routines tick` / `routines run` / `health check` (info and above) | 5 MB x 5 (`monitoring.log`) |
| `data/logs/routines-tick.log` | Report and stderr from the cron wrapper, per tick | 5 MB x 3 |
| `data/logs/health-check.log` | Report from the launchd wrapper, per check | 5 MB x 3 |
| `data/logs/health-check.launchd.log` | launchd stdout/stderr (normally empty) | none needed |

### 5.5 Run-id correlation (PLAN §6.12)

A single set of ids follows the work from the Hermes cron, through the
dispatcher, to Slack:

- The cron wrapper mints `ARC_TICK_ID` and sets `ARC_CRON_JOB`.
- `HERMES_KANBAN_TASK` / `HERMES_SESSION_ID` are picked up when they are present.
- The dispatcher binds `run_id`, `chain_run_id`, `job` and `step_index` for each run.

All of these ids are bound to every JSON log line and stored on the heartbeat.
Failure alerts end with the `run_id`, ops alerts show `tick_id`/`check_id`, and
a crashed tick's cron alert prints `arc health trace <tick_id>`.

```
arc health status                 # last tick/health heartbeat, open alerts
arc health check --no-slack       # run the checks now, alerts to the log only
arc health trace <id>             # tick_id | run_id | chain_run_id | alert id | Slack ts
```

`trace` lists the matching heartbeats, the routine runs (including every run of
a traced tick), the alerts, and the JSON log lines, current and rotated files
alike.

### 5.6 Control tower (E8.3)

A read-only Streamlit dashboard over the audit store, served on the host's
Tailscale address, port 8501. Code: `arc/tower/`.

```
arc tower serve                   # http://<tailscale-ip>:8501, re-reads every 30s
arc tower serve --local           # 127.0.0.1:8501, this machine only
arc tower serve --print-command   # show the streamlit argv, don't start
arc tower snapshot [--json]       # the same data as text/JSON, no server
```

**Bind rules.** The address is `tailscale ip -4` (CLI on PATH or the macOS
app bundle), otherwise the first `100.64.0.0/10` address on any interface.
`--address` accepts only a Tailscale or loopback IP. With no Tailscale address,
`serve` exits 2 rather than fall back to `0.0.0.0` or the LAN.

**Read-only.** The DB is opened with `mode=ro` + `PRAGMA query_only`, and a
missing DB is an error, not created. The page has no buttons or inputs. Approve,
halt, resume and config stay in Slack. The tower never calls the broker, market
data or an LLM. Streamlit runs headless, with XSRF protection on and usage stats off.

| Section | Source |
|---|---|
| Status banner | active `halts`, latest `tick`/`health` heartbeats, open `ops_alerts` |
| P&L | latest `monitor` heartbeat (intraday equity, day P&L); `pnl_snapshots` (reconciled realized/unrealized, Day/MTD/YTD via `arc.reconcile.performance`) |
| Greeks | latest `monitor` heartbeat: net Δ Γ ν Θ and max loss, against the gate's `portfolio_delta_cap` / `portfolio_vega_cap_pct` |
| Positions | `open_structures` + broker legs from the `monitor` heartbeat; "held at broker" from the last `positions_snapshots` |
| Proposals | `proposals` + latest `gate_decisions` + `approval_requests` + `executions` (last 7 days) |
| Halts | `halts`, active first |
| Gate violations | failed `gate_decisions`, split by rule code (last 7 days) |

The intraday `monitor` routine writes one `heartbeats` row per run
(`component = monitor`: Greeks, equity and broker legs). Greeks older than 45 minutes
are flagged as stale on the page. Outside the session they show the last in-session run.

## 6. Local Models (E8.4)

Placeholder — populated by card E8.4 when the 128 GB Mac Studio arrives.
See PLAN.md §2.4 for target: Scout/Investor/Auditor (cheap tier) routed
locally via llama.cpp or omlx server; that is a `tiers.cheap.model` edit in
`config/llm_routing.yaml` once Hermes has a local provider.
