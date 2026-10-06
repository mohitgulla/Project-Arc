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
| **cheap** | Scalp | `anthropic/claude-opus-5` |

D56 (E13.2): the Broker (order ladders, post-market reconcile) and Ops (weekly scorecard)
are deterministic jobs, not LLM personas, so they have no tier.

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
  sweep:    cheap
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
--ignore-rules -t todo`) via `arc.ingest.llm.HermesSweepLLM`. No call site
names a model:

- Sweep (`arc ingest` / `run_sweep`): `HermesSweepLLM.from_settings(settings)`
  → persona `sweep`.
- Director / Quant / Risk (`arc propose`, `PipelineEnv.live`):
  `HermesSweepLLM.from_settings(settings, persona, timeout_seconds=...)`, one
  backend per persona.

The model that actually answered is read back from the Hermes usage file and
stored with each persona reply for audit.

### 2.3 Cost estimation (per pipeline run)

| Persona | Calls/run | Tokens/call (est.) | Tier |
|---------|-----------|-------------------|------|
| Sweep | 1-5 | ~2K in / ~1K out | cheap |
| Director | 1 | ~4K in / ~2K out | frontier |
| Quant | 1-3 | ~4K in / ~3K out | frontier |
| Risk | 1 | ~3K in / ~2K out | frontier |

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
| `ALPACA_API_KEY` | Alpaca paper trading (production paper account) | Yes |
| `ALPACA_SECRET_KEY` | Alpaca paper trading (production paper account) | Yes |
| `ALPACA_BASE_URL` | Alpaca API endpoint | Yes |
| `ALPACA_TEST_API_KEY` | Dedicated test paper account, integration tests only (§5.12, E6.2c) | Owner |
| `ALPACA_TEST_SECRET_KEY` | Dedicated test paper account, integration tests only (§5.12, E6.2c) | Owner |

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
| `arc routines tick` | Hermes cron `arc-routines-tick`, `*/10 * * * *` on the clock (D52; `hermes/routines/install.sh`) | Records one `tick` heartbeat per live tick with its `tick_id`, outcome counts and run ids. A crash still records a `failed` heartbeat. |
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
| tick_slow (E8.2a) | In the last `coverage_window` (60m), at least `tick_slow_count` (2) ticks took longer than `tick_slow_after` (4m, from E5.10's `tick_duration_ms`), or the p90 gap between ticks is above 1.5 × `tick.interval` (7m30s). The message names the slowest job. | `tick_slow` |
| routine_windows | Only for **slow-cadence** jobs (slots at least `per_slot_min_interval`, 60m, apart: Sweep overnight, broker.reconcile, earnings, the daily sources). A scheduled slot's catch-up window (+`miss_grace` 10m) closed and the slot never ran or was recorded as missed. Slots are only judged after the first tick heartbeat, looking back `miss_lookback` (1d). | `missed:<job>:<slot>` |
| slot_coverage (E8.2a) | For **fast** jobs (the 10-min loop, monitor, the 30-min Sweep, rss/edgar): the job ran fewer than `coverage_min` (80 %) of its slots judged in the last `coverage_window` (60m). Slots are judged the same way as routine_windows (collapse aware), and halted slots count in neither number. One alert per job, which names the likely cause from the tick heartbeats (slow ticks with the top job, or tick gaps). | `coverage:<job>` |
| earnings_coverage (E4.1d) | The `earnings` source job is enabled, the effective universe has at least one non-ETF ticker, and no earnings-calendar doc was stored in the last `earnings_stale_after` (7d). The message names the last earnings run's status and error (e.g. `skipped: no_api_key`, `failed: HTTPError …`). Without the dates `next_earnings` is empty and short premium on stocks fails closed. Never folded into a tick incident. | `coverage:earnings` |
| stuck_runs | A `routine_runs` row is still `running` after `stuck_after` (70m), or after its job's `stuck_after_jobs` override (`monitor: 10m`, E5.3a). | `stuck:<run_id>` |
| gateway | `hermes gateway status` or `hermes cron status` shows a `✗`, exits non-zero, or times out. `⚠` warnings count as degraded: they are recorded but not alerted unless `gateway.alert_on_degraded: true`. | `gateway` |
| remote_access (E8.6, on since 2026-10-03) | `GET <ts-ip>:1994/api/status` doesn't answer, or answers without `auth_required: true` and `basic` in `auth_providers`; `GET <ts-ip>:4174/api/health` isn't 200 with `status: ok`; or either port accepts a connection on a LAN address. See §5.7. | `remote_hermes`, `remote_tower`, `remote_exposed` |

Some skips are deliberate and are never counted as misses: halted personas and
jobs with no handler yet. Job failures are already alerted in the #arc-investor
day thread, and those alerts now end with the run id.

### 5.3 Alerts

Alerts are posted to `#project-arc` (`monitoring.alert_channel`) as one message
per check run:

- A condition alert (tick, slow ticks, slot coverage, stuck run, gateway) is
  posted once when it opens.
- While the condition persists, it is not posted again.
- When the check passes again, a `resolved` line is posted. It describes the
  current state (e.g. "routines tick heartbeat is fresh again (last tick 2 min
  ago, tick-…)"), not the text the alert opened with.
- A missed routine window of a slow-cadence job is posted exactly once. Several
  missed slots of the same job in one post collapse into one line per job.
- A fast job never gets one alert per slot (E8.2a). A degraded 10-min loop shows
  up as one `coverage:director` alert, e.g. "director ran 7/12 slots in the last
  60 min (58%) · likely cause: slow ticks (max 8m03s, sweep 5m40s)". It
  resolves with the recovered ratio: "director slot coverage recovered: ran
  12/12 slots in the last 60 min (100%)".
- Outages don't flood the channel. While a `tick_stale`, `gateway` or `tick_slow` incident is
  open, or opens in the same run, any missed slot whose window closed during
  it (from the last good tick onward) is recorded with
  `correlation.folded_into = <incident alert id>` and is not posted on its own.
  The incident's resolve line summarises them, e.g. "during it 23 routine
  slot(s) missed: rss ×18, edgar ×4, director ×1". A slot judged after the
  incident resolved goes out as one thread reply under the incident post. A
  full outage therefore produces two root posts: one when it opens and one
  when it resolves.
- Coverage alerts fold the same way. A `coverage:<job>` that opens while one of
  those incidents is open is recorded but not posted, and its own resolve is
  silent. The incident's resolve line lists it ("low slot coverage: director,
  monitor"). If the incident clears but the job still misses slots, the
  coverage alert is posted then, on its own.
- Per-slot detail for the fast jobs is in the tower's Ops page and in one
  line in the Broker's 16:30 reconcile card (`Ops` section): `Slots: director
  71/75, monitor 77/78, sweep 15/15 · missed 6 (list in tower Ops)`.
- Every threshold above lives under `monitoring:` in `config/routines.yaml` and
  can be changed from Slack (`!arc config set monitoring.coverage_min 70%`, §5.8).
  Setting `monitoring.per_slot_min_interval` to 5 gives back the old
  one-alert-per-slot behaviour for every job. `arc health trace <alert id>`
  works for both `missed:` and `coverage:` alerts.

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

### 5.6 Control tower (E8.3 → v2 E8.7, D35)

A read-only web app over the audit store: a FastAPI JSON API plus a React SPA
(design spec: `docs/TOWER_DESIGN.md`), served by uvicorn on the host's Tailscale
address, port 4174. It replaced the E8.3 dashboard at the E8.7e cutover (D35).
Code: `arc/tower/` (`api.py`, `routes/`, `data*.py`), `web/` (SPA source).

**Build and serve.** The SPA is built once per deploy; Node is a build-time tool only,
not needed at runtime (uvicorn serves the built files from `arc/tower/static/`, which is
gitignored). CI's `web` job builds the same files and uploads them as the `tower-static`
artifact.

```
make web                               # build the SPA into arc/tower/static/ (needs Node >= 22.12)
arc tower serve                        # http://<tailscale-ip>:4174, uvicorn
arc tower serve --local --port 4180    # 127.0.0.1 only, this machine
arc tower serve --refresh 30           # client poll interval: 30 | 60 (default) | 120 s
arc tower serve --print-command        # show the uvicorn argv, don't start
arc tower snapshot [--json]            # the /api/snapshot payload as text/JSON, no server
curl http://<addr>:4174/api/health     # {status: "ok", db, as_of}
```

**Deploy (this Mac):** `git pull && uv sync --locked && make web &&
hermes/remote/install.sh` (§5.7). `install.sh` refuses to install the tower agent until
`arc/tower/static/index.html` exists ("run `make web` first"). After a later `make web`
the running agent serves the new files on the next page load; no restart is needed.
After a Python change, `launchctl kickstart -k gui/$(id -u)/com.projectarc.tower`.

**Pages** (bottom tab bar on mobile, sidebar on desktop):

| Page | Route | API |
|---|---|---|
| Overview (E8.7a) | `/` | `/api/overview?range=1D\|1W\|1M\|3M\|YTD\|ALL`, `/api/snapshot` |
| Trades (E8.7b) | `/trades`, `/trades/<proposal_hash>` | `/api/trades`, `/api/trades/filters`, `/api/trades/{hash}`, `/api/search?q=` |
| Positions (E8.7a) | `/positions` | `/api/positions?status=open\|closed\|all` |
| Performance (E8.7c) | `/performance` | `/api/performance`, `/api/performance/breakdown?by=…` |
| Ops & pipeline (E8.7d) | `/ops`, `/ops/runs/<run_id>`, `/ops/context/<entry_id>` | `/api/ops/{session,health,alerts,halts,runs,runs/{id},budget,context,context/{id},sources,llm,config}` |

Plus `/settings` (theme, density, refresh; client-side only) and `/kitchen-sink` (every
design-system component in both themes). Shell data for every page: `/api/health`,
`/api/meta` (version, git sha, `ARC_ENV`, account profile, `config_version`,
`monitor`/`broker.reconcile`/`tick` cadences with stale thresholds, gate caps). `/api/docs` is the
OpenAPI browser. Every response carries `as_of` (ET); errors are `{error, detail, as_of}`
(e.g. 503 `db_unavailable`, 503 `config_unavailable`).

**Bind rules (D29).** The address is `tailscale ip -4` (CLI on PATH or the macOS
app bundle), otherwise the first `100.64.0.0/10` address on any interface.
`--address` accepts only a Tailscale or loopback IP. With no Tailscale address,
`serve` exits 2 rather than fall back to `0.0.0.0` or the LAN. It also exits 2 when the
DB is missing.

**Read-only.** Each request opens the DB `mode=ro` + `PRAGMA query_only`, and a missing
DB is an error, never created. The app has no POST/PUT/DELETE route (a test checks the
OpenAPI spec; a `POST` gets 405), and the import-linter contract `arc.tower is read-only`
forbids the broker, market data, personas, LLM routing, Alpaca and Slack. Approve, halt,
resume and config stay in Slack. D26 config overrides are read from the store on every
request, so a Slack `!arc config` change shows without a restart.

**No auth, no CORS.** The tailnet-only bind is the boundary. Don't bind it anywhere else.
**Offline:** Inter is bundled (`@fontsource/inter`); the page fetches nothing outside the tower.

**Freshness rule (D35).** No number is shown without its age: every response carries
`as_of`, and each section carries the timestamp of the row it came from. The SPA polls
every 60 s (30/60/120 in Settings), paused while the tab is hidden. There is no faster
feed: the data moves only when a routine writes, and the tick floor is 5 minutes.

**Stale thresholds** come from the effective routines config (`routines.yaml` plus
`!arc set` overrides, via `/api/meta`), never constants, so a cadence change moves them
with no code change:

| Data | Stale after |
|---|---|
| Monitor marks (Greeks, intraday equity, broker legs) | 3 × `personas.monitor.every` (30 min at 10m); `greeks.stale_after_s` in the snapshot |
| `broker.reconcile` and other cadenced jobs in `/api/meta` | 3 × the job's cadence |
| Tick | 3 × the tick interval (30 min at 10m); the Ops health strip uses `monitoring.tick_stale_after` |
| Health check heartbeat | 3 × the LaunchAgent's 30 min |

Outside the session the pages show the last in-session monitor run, marked stale.

**Where the numbers come from:**

| Section | Source |
|---|---|
| Status strip | active `halts`, latest `tick`/`health` heartbeats, open `ops_alerts` |
| P&L / equity | latest `monitor` heartbeat (intraday equity, day P&L); `pnl_snapshots` (reconciled realized/unrealized, Day/MTD/YTD via `arc.reconcile.performance`) |
| Greeks | latest `monitor` heartbeat: net Δ Γ ν Θ and max loss, against the gate's `portfolio_delta_cap` / `portfolio_vega_cap_pct` |
| Positions | `open_structures` + broker legs from the `monitor` heartbeat; "held at broker" from the last `positions_snapshots` |
| Proposals / Trades | `proposals` + latest `gate_decisions` + `approval_requests` + `executions` |
| Halts | `halts`, active first |
| Gate violations | failed `gate_decisions`, split by rule code |
| Ops | `routine_runs`, `run_manifests`, `heartbeats`, `ops_alerts`, `context_entries`, the D32 local order count, D26 control tables |

The intraday `monitor` routine runs every 10 minutes in session (D35/D52, was 5; `personas.monitor`
in `config/routines.yaml`) and writes one `heartbeats` row per run (`component = monitor`):
Greeks, `equity`, `last_equity`, `cash`, `buying_power`, `options_buying_power`,
`non_marginable_bp`, `broker_requests` (the run's request estimate), and the broker legs
with `current_price`, `lastday_price` and `change_today` when Alpaca reports them. The
tower reads only this row, so marks are at most ~11 minutes old in session.

Broker load: each run makes 2 + 4 × (open underlyings) Alpaca requests (account, positions,
then quote, chain snapshot, contracts page and volume snapshot per underlying): 34 at the
default 8 open positions, one run per 10 minutes, far below Alpaca Basic's 200 requests/min.

Quick check of the fields on a DB:

```
sqlite3 data/arc.db "select at, json_extract(detail,'$.equity'), json_extract(detail,'$.buying_power') from heartbeats where component='monitor' order by at desc limit 3"
```

**Dev:** `make web-check` (eslint, tsc, vitest), `make web-api` after changing a route or
response model (regenerates `web/openapi.json` and the typed client; a pytest fails if they
drift), `make web-e2e` (Playwright screenshots at 390×844, 768×1024 and 1440×900 in both
themes, against a scratch DB; set `PLAYWRIGHT_BROWSERS_PATH` to a dir with Chromium, e.g.
`~/.hermes/tools`). `.venv/bin/python scripts/tower_fixture_db.py <new.db> [--now ISO]
[--history] [--ops]` builds a populated fixture store anchored at *now* (3 open + 1 closed
structure, 10 reconciled days, 30 monitor marks, proposals in every status, a halt, alerts;
`--history` adds ~40 closed trades over 3+ months, `--ops` the Ops page's runs and
manifests); it refuses to overwrite an existing file. Serve it with
`arc tower serve --local --db <new.db>` to look at a full tower by hand.

**Page notes:**

- **Overview (E8.7a):** `GET /api/overview?range=1D|1W|1M|3M|YTD|ALL` (status strip,
  equity + range series, day P&L, positions, Greeks vs caps, 24 h proposals, movers,
  last 20 activity lines) and `GET /api/positions?status=open|closed|all`. `1D`
  plots today's monitor marks; other ranges plot reconciled daily closes.
- **Trades (E8.7b):** `/trades` lists every proposal (opens and closes). Filters,
  sort and paging all live in the URL, so a filtered view can be shared, e.g.
  `/trades?stage=gate_fail&date=7d&sort=net_ev`. The summary strip is computed over the
  whole filter, not just the current page. `/trades/<proposal_hash>` is the drill-down:
  header, payoff, quant, decision trail + persona calls, gate, approval, execution + order
  events + fills, position & exits (close-to-reallocate links), outcome & review, market
  context + regime snapshot + Sweep candidate, and the run manifest. The API routes are
  `GET /api/trades?<filters>&page&size&sort&dir`, `/api/trades/filters`,
  `/api/trades/{hash}` and `GET /api/search?q=` (ticker, hash prefix, run id, chain id,
  structure id; it backs the header search). Migration 017 adds only the indexes the
  list query needs (<220 ms with filters on a 100k-proposal store). The gate token is
  never served; only its version is.
- **Performance (E8.7c, E8.8c):** `/performance?range=1D|1W|1M|3M|YTD|ALL` (default 3M,
  URL-synced; `by=` picks the breakdown tab, `shadow=true` the D19 overlay). The range maps to
  the API preset 1D→`1d`, 1W→`7d`, 1M→`30d`, 3M→`90d`, YTD→`ytd`, ALL→`all`; the page sends
  only `preset` (no period comparison, paper test legs always left out). API:
  `GET /api/performance?preset=1d|7d|week|mtd|qtd|ytd|30d|90d|all|custom&from&to&compare=prev|yoy|none&include_tests=true`
  (every card; `compare` defaults to `none` and is kept for back-compat only, TODO: drop it in
  a cleanup PR) and
  `GET /api/performance/breakdown?by=ticker|structure|exit_reason|reason_code|profile|regime`.
  Responses are cached in memory for 60 s per (query, DB file mtime/size), so a poll
  doesn't re-scan the journal and any write to the store misses the cache. Every number comes
  from the weekly scorecard's functions (`arc.journal.scorecard.closed_positions`,
  `execution_costs`, `funnel`, `model_vs_realised`, `calibration_points` +
  `arc.journal.attribution.calibration`), trade stats from `arc.journal.tradestats`, and
  equity stats from `arc.reconcile.performance` (`daily_equity`, `drawdown`, `sharpe`, `sortino`,
  `period_return`). The tower and the Friday scorecard agree by construction. **Paper
  test legs:** a trade whose open or close has an `arc-<hex>` client id (the broker
  smoke test, not an `arc2.` ladder attempt) is left out of every trade card, and its
  realised P&L is taken out of the equity-based Net P&L (the API's `include_tests=true` puts
  them back; the page no longer offers it). `scripts/tower_fixture_db.py <new.db> --history` adds ~40 closed trades over
  3+ months (shadows, reviews, fees, a smoke-test trade) for the page's tests and e2e.
  Definitions (the page shows a one-line summary of each and the short form in an ⓘ tip):
  - **Net P&L**: change in daily closing equity over the period (realised + unrealised),
    minus test-leg realised P&L; when there are no equity closes, the realised P&L of
    trades closed in it. Bars by day up to 45 days, else by week (≤ 400 days) or month.
    The shadow overlay adds Σ(D19 hold-to-expiry shadow − realised) of closed trades.
  - **Sharpe**: mean ÷ sample standard deviation of daily close-to-close equity
    returns × √252. Risk-free rate 0; deposits/withdrawals are not netted out; needs ≥ 2
    returns with non-zero dispersion, else "—". The window includes the close before
    the period's first day.
  - **Sortino**: mean ÷ downside deviation of the same daily returns × √252, where
    downside deviation = √(mean of min(r, 0)²) over all returns (target 0, the
    experiments' definition). Needs ≥ 2 returns and at least one losing day, else "—".
  - **Max drawdown**: largest fall of a daily closing equity from any earlier peak in the
    window, in $ and as a fraction of that peak; *recovered* = first later close at or
    above the peak.
  - **Return**: last close in the period ÷ the close before its first day − 1 (the first
    close in the period when there is none before).
  - **Win / loss**: a win is realised P&L > 0; a $0 scratch counts as a loss (as in
    `PnlSummary`). Win rate = wins ÷ closed; profit factor = Σ wins ÷ |Σ losses| ("—"
    with no losing dollars); **expectancy** = Σ realised ÷ trades closed
    (= win rate × avg win + loss rate × avg loss). Fills only: commissions and fees are
    not deducted (they're on the Costs card). Days held = closed − opened.
  - **Costs**: commission and regulatory fees (ORF, OCC, CAT, TAF, SEC) from the fee
    model stored with each fill's proposal (a close without its own uses its open's);
    spread = the cost model's expected crossing cost; slippage = fill vs mid beyond it.
    Cost % of gross = total ÷ |Σ realised|.
  - **Modelled vs realised (D23)**: managed-exit net EV × contracts per closed trade vs
    its realised P&L; PoP hit rate = realised win rate vs mean managed PoP, and the
    hold-to-expiry variant = share of known D19 shadows > 0 vs mean static PoP.
  - **Calibration (E7.4)**: persona confidence (Director) and the Quant's PoP bucketed
    against the realised win rate over every closed trade to the period end.
- **Ops & pipeline (E8.7d):** the session timeline is `config/routines.yaml` slots
  (06:00–22:00 ET) joined to root `routine_runs`; missed slots are `--warn`. Run detail shows
  the stored D27 manifest and the `arc context trace` view (`arc.context.trace.trace_runs`),
  with undeclared reads/writes highlighted. The order budget is the local D32 count only (the
  tower never asks the broker). Config goes through `ControlService` reads.

### 5.7 Remote access over Tailscale (E8.6, D29)

Use Hermes (chat, sessions, cron, kanban, config) and the control tower from a
laptop or phone on the **same Tailscale tailnet** as the Mac Mini. Nothing listens
on the LAN or the internet.

| Service | URL | LaunchAgent | Runs |
|---|---|---|---|
| Control tower v2 (read-only, FastAPI + React, §5.6) | `http://<ts-ip>:4174` (health: `/api/health`) | `com.projectarc.tower` | `.venv/bin/arc tower serve` → uvicorn `arc.tower.serve:app_from_env` on `<ts-ip>:4174`; serves the SPA built by `make web` |
| Hermes dashboard (web admin, Chat tab, Hermes Desktop backend) | `http://<ts-ip>:1994` | `com.projectarc.hermes-dashboard` | `hermes/remote/run_dashboard.sh` → `arc remote dashboard` → `hermes dashboard --host <ts-ip> --port 1994 --no-open --skip-build` |

Both agents are `KeepAlive` + `RunAtLoad`, log to `data/logs/{hermes-dashboard,tower}.launchd.log`,
and use the repo `.venv` (not `/usr/bin/python3`). Code: `arc/remote/`, `hermes/remote/`.

**What the code enforces**

1. **Bind.** Both services bind only what `arc.tower.net.resolve_bind_address` returns:
   the Tailscale `100.64/10` IPv4 (or `127.0.0.1` with `--local`). `0.0.0.0`, LAN IPs and
   hostnames are refused. No Tailscale address → exit 2 ("no Tailscale IPv4 address
   found"), and launchd retries every 60s (`ThrottleInterval`). Nothing widens it.
2. **Login.** `arc remote dashboard` refuses to start unless `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`,
   `..._PASSWORD_HASH` and `..._SECRET` are set, and refuses a plaintext `..._PASSWORD`.
   Hermes itself also refuses a non-loopback bind without an auth provider. Only key
   names are ever printed.
3. **One copy.** Before starting it runs `hermes dashboard --status` and also probes
   the port on loopback and the bind address. The port probe is needed: `--status`
   matches process argv, and a dashboard started through the source launcher
   (`python3 -I -c …`) is **not** listed by `--status` on this install (verified).
4. **Isolation.** `hermes/remote/install.sh` only ever boots out its own two labels. It
   never touches `ai.hermes.gateway` (Slack) or `com.projectarc.health-check`.

**Verified on this host (2026-09-28, Hermes 0.21.5)**

- *Dashboard extras: nothing to install.* The `hermes` launcher runs `hermes_bootstrap`,
  which activates the Hermes-managed dependency venv (`~/.hermes/installs/<id>/environments/<env>/venv`).
  That venv already has `fastapi 0.133.1`, `uvicorn 0.41.0`, `starlette 1.3.1` and
  `ptyprocess 0.7.0`, and the built UI is in `hermes_cli/web_dist`. The bare runtime
  interpreter (`~/.hermes/tools/python-3.14.7…/bin/python3 -I`) can't import them, which is
  expected: never import them from it directly. Do **not** run `uv pip install -e ".[web,pty]"`:
  it targets the wrong environment, and `pty` is now an empty back-compat extra.
  `hermes dashboard --host 127.0.0.1 --port 1994 --no-open --skip-build` served
  `/api/status` (`version 0.21.5`). If the imports ever break, Hermes' own message points to
  `hermes pm install` / `hermes pm repair`. (`hermes pm status` currently crashes on this
  install with a missing `pm/uv.lock`; that is a Hermes issue, unrelated to the dashboard.)
- *Host header.* The dashboard accepts the address it bound to (`http://<ts-ip>:1994`) plus
  exactly one name from `dashboard.public_url` in `~/.hermes/config.yaml`. This host trusts
  `http://mac-mini:1994` (set 2026-10-03); `http://mac-mini.<tailnet>.ts.net:1994` still returns
  **400 "Invalid Host header"** unless `public_url` is switched to that name. Restart the
  dashboard after changing it (`kill` the pid; launchd respawns within ~60 s).
- *Gate (loopback smoke test, basic provider):* `/api/status` →
  `{"auth_required": true, "auth_providers": ["basic"], "auth_flows": ["cookie", "native_pkce"]}`;
  `POST /auth/password-login` with a wrong password → `401 {"detail":"Invalid credentials"}`;
  `GET /api/sessions` without a session → 401; WebSockets `/api/pty` (Chat tab), `/api/pty?token=…`
  and `/api/ws` without a login ticket → handshake rejected (HTTP 403).

**Owner runbook** (the owner runs these)

1. **Tailscale on the Mac Mini.** Install the standalone macOS app from
   https://tailscale.com/download/mac (Homebrew isn't installed here). Sign in and turn on
   "Launch at login". In the menu-bar app: Settings → "Install CLI", so `tailscale` is on
   PATH. Check: `tailscale status` and `tailscale ip -4` (a `100.x.y.z` address).
2. **Tailscale on the laptop/phone** with the same account. In the admin console
   (https://login.tailscale.com/admin/machines) **disable key expiry** for the Mac Mini so it
   doesn't drop off the tailnet after 180 days. Do not enable Funnel.
3. **Dashboard extras:** nothing to do (see above). Optional check:
   `hermes dashboard --status` prints "No hermes dashboard or serve processes running."
4. **Dashboard login:** `~/GitHub/Project-Arc/hermes/remote/set-password.sh`
   Prompts for a username (default `$USER`) and a password twice (hidden, ≥ 12 chars).
   It hashes the password with Hermes' `hash_password` (scrypt), writes
   `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `..._PASSWORD_HASH`, adds `..._SECRET` only if it is
   unset, removes any plaintext `..._PASSWORD`, keeps `~/.hermes/.env` at `0600`, and prints
   key names only. Re-run it to change the password.
5. **Start both services:** `cd ~/GitHub/Project-Arc && make web && hermes/remote/install.sh`
   (`make web` builds the tower's web app; install refuses without it.)
   (`--print` shows the plists, `--uninstall` removes both.) It prints each agent's
   `state`/`last exit code`; `state = running` means it is up.
6. **Turn on the health check:** set `monitoring.remote_access.enabled: true` in
   `config/routines.yaml` (or via `!arc set` once E8.5 lands). The next 30-min health run
   alerts `remote_hermes` / `remote_tower` / `remote_exposed` to #project-arc once each.
7. **Use it** (`<ts-ip>` = output of `tailscale ip -4`):
   - Tower: `http://<ts-ip>:4174`
   - Hermes in a browser/phone: `http://<ts-ip>:1994` → sign in → Chat tab
   - Hermes Desktop: Settings → Gateways → Add connection → Remote gateway → URL
     `http://<ts-ip>:1994` → Sign in
8. **Optional shell access:** System Settings → General → Sharing → Remote Login (limit it to
   your user), then `ssh mohit.gulla@<ts-ip>` from a tailnet device. Hermes Desktop's "SSH"
   connection kind can use that too.

**Troubleshooting**

| Symptom | Cause / fix |
|---|---|
| Login returns 401 / "Invalid username or password." | Wrong username or password. Re-run `set-password.sh`, then `launchctl kickstart -k gui/$(id -u)/com.projectarc.hermes-dashboard` (the running process only reads `.env` at start). |
| No sign-in button / "no auth provider" | The basic provider didn't register: a key is missing or blank. `data/logs/hermes-dashboard.launchd.log` names the missing key. Run `set-password.sh` and kickstart. |
| Signed out after every restart | `HERMES_DASHBOARD_BASIC_AUTH_SECRET` is missing, so Hermes signs sessions with a random per-process key. `set-password.sh` adds one if unset. Don't delete it. |
| Connection refused | Check `tailscale ip -4` on the Mini and that the client device is on the tailnet. `launchctl print gui/$(id -u)/com.projectarc.hermes-dashboard` (or `.tower`): `last exit code = 2` → read the launchd log (no Tailscale address, login not configured, or port 1994 already in use by another dashboard). |
| 400 "Invalid Host header" | You used the machine name. Use `http://<ts-ip>:1994`, or set `dashboard.public_url` (see above). |
| Tower log: "audit store not found" | `data/arc.db` doesn't exist yet in the repo the agent runs from; the tower never creates it. |
| `install.sh`: "arc/tower/static/index.html missing", or the tower page says "the web app is not built" (503) | The SPA isn't built in that checkout (`arc/tower/static/` is gitignored). Run `make web` (Node >= 22.12), then re-run `install.sh` or just reload the page. The API (`/api/*`) works without it. |
| `/api/health` 503 (`remote_tower` alert) | The tower is up but can't read the store: `db_unavailable` = `data/arc.db` missing or unreadable (permissions, a half-restored backup); the body's `detail` names the path. Other `/api/*` 503s with `config_unavailable` mean `config/routines.yaml` or the D26 tables can't be read. Fix the file, no restart needed. A connection error instead of 503 means the agent isn't running (`launchctl print gui/$(id -u)/com.projectarc.tower`). |

The live check from a second tailnet device (tower loads, dashboard login, Chat tab
reply, Desktop "Remote gateway" connects, `remote_access` green) is owner-gated and
runs after steps 1–6.

### 5.8 Control panel (E8.5, D26)

Owner-only runtime config from Slack (`@hermes !arc …`) or the shell (`arc config …`).
The plugin shells out to the CLI; the CLI owns every rule.

| Slack | CLI | What |
|---|---|---|
| `!arc config [group\|key]` | `arc config show [group\|key]` | grouped summary; detail = value, default, bounds, hard ceiling, last change |
| `!arc set <key> <value> [-- reason]` | `arc config set <key> <value> [--reason R]` | change a key |
| `!arc profile <name>` | `arc config profile <name>` | shortcut for `account_profile` |
| `!arc diff` | `arc config diff` | overrides vs the file/env default |
| `!arc history [key]` | `arc config history [key]` | change log, newest first |
| `!arc revert <change_id\|key>` | `arc config revert <ref>` | undo a change / reset a key |
| `!arc confirm <code>` / `cancel <code>` | `arc config confirm\|cancel <code>` | riskier-change confirm step (also buttons) |

- **Registry** (`arc/control/registry.py`, `arc config keys`): every tunable has a type,
  bounds/choices, risk direction and a code-level hard ceiling Slack cannot exceed.
  Never tunable: `ARC_ENV`, gate code, secrets, paths, `config_version`.
  `!arc config` lists every key grouped (account, universe, risk, entries incl. each
  profile's DTE window, exits, positions, execution, costs, approvals, routines);
  categorical keys show their options inline, e.g. `account_profile = cash_debit
  (options: cash_long_only | cash_debit | margin)`.
- **Adding a knob** (any card): add the `ArcSettings` field / YAML path to the registry,
  or list it in `NOT_EXPOSED` with a reason. `tests/test_control.py::
  test_every_setting_is_classified_tunable_or_not` fails otherwise. Code that reads
  exits/costs must go through `arc.control.effective.exit_config|cost_model(settings)`,
  not `load_exit_config()`, or Slack overrides won't reach it.
- **Owner-only**: the Slack user id (from the gateway event, never message text) must be
  in `ARC_APPROVER_SLACK_USER_IDS` from `~/.hermes/.env`. A local shell (`--actor local`)
  counts as the owner. Refusals are logged.
- **Riskier-direction** changes (raise a cap, loosen/remove a stop, `margin`, add a
  ticker, `auto_approve.paper|live` on, disable a routine) create a pending change with a
  6-char code and a 10-minute TTL; safer/neutral changes apply immediately.
- **Audit**: `config_changes` is append-only (triggers); reverts are new rows. The latest
  id is the `config_version` every `routine_runs` row and run manifest records.
- **Applies at the next tick**, no restart: the tick, `arc propose`, approvals,
  execution, reconcile and exits read `arc.control.effective_settings()` (defaults <
  YAML/env < DB overrides); YAML keys (exits, costs, profiles, routines) patch the raw
  YAML before pydantic validation.
- **Install / update the plugin** (repo is the source of truth):
  `cp -r hermes/plugins/arc-status ~/.hermes/plugins/`. The slash command reloads live;
  the Confirm/Cancel buttons need one gateway restart (when no kanban worker runs).
  Without the restart, type `!arc confirm <code>`.

### 5.9 Daily options order budget (E6.5, D32)

At most `order_budget.daily_max` broker option-order submissions per ET day (default and
code hard ceiling 200). Every ladder attempt counts, filled or cancelled, opens and closes,
plus orders placed by hand in the Alpaca dashboard (the count is `max(local orders rows,
broker order list)` plus the remaining attempts of ladders still working).

| Tier | When (defaults) | Effect |
|---|---|---|
| normal | used < `restrict_at` (100) | none |
| restrictive | used ≥ 100 | Quant/Risk budget (`pipeline_max_shortlist`) ≤ 1 (Director still ranks all), ≤ 1 new open per loop, managed Net EV ≥ 1.5× round-trip costs, PoP ≥ breakeven + 5pp, ≤ 2 improvement steps; E5.9's idea-dedupe cooldown reads `arc.budget.effective_cooldown` (×2) |
| opens_exhausted | used ≥ `daily_max − close_reserve` (175) | no new opens (entry chain stops before any LLM call); closes still run |
| exhausted | used ≥ 200 | nothing goes to the broker, closes included; use the Alpaca dashboard |

Enforced twice, deterministically: the gate rule `order_budget` (charges the band's worst-case
attempts) and the ladder guard (re-counts before every attempt, journal `order:budget_exhausted`).
Each tier crossing posts one notice in the day thread. `!arc budget` / `arc budget status
[--db …] [--day …] [--json]` shows used/limit/tier and the local-vs-broker cross-check; the
tower's ops strip and the monitor heartbeat carry the same numbers. Tunables:
`order_budget.daily_max|restrict_at|close_reserve` plus the `order_budget.restrictive.*`
knobs (see `!arc config execution`). Raising the cap is the riskier direction (confirm step).

### 5.10 Auto-approve and in-chain Execute (E6.6, D34)

`auto_approve` is one switch per environment, both **off** by default. When it is on for the
running `ARC_ENV`, the chain step `broker.execute` (right after `propose`, and after
`risk.reallocate` in the position-manager chain) publishes that chain's proposal cards,
approves them as `arc:auto-approve` (card marked `Auto-approved (paper|LIVE)`, no buttons),
and hands each one to a Broker subprocess (`arc routines run broker --event <id>`,
its own per-event lock, never the LLM lock), so the ladder starts in the same tick. When it
is off, `broker.execute` reports `awaiting approval (N cards)` and the click flow is unchanged.

Not relaxed by the switch: the gate, the order budget (§5.9), the daily-loss halt, `!halt`,
and the GateToken + ApprovalRecord requirement in `submit()`. The ApprovalRecord's approver
is `arc:auto-approve`; the journal row carries `env`.

Freshness (`max_quote_age`, default 60 s, ceiling 600): when the proposal is older than
that at the first attempt, the ladder re-prices at the current mid and re-anchors the band
there (`PriceBand.reanchor`, pure gate code; the band is never widened). A mid outside the
signed band sends nothing (journal `order:stale_band`); the next loop may propose afresh.

Runbook (paper):
1. `cd ~/GitHub/Project-Arc && .venv/bin/arc approve auto on --reason "paper loop"`
2. `.venv/bin/arc approve auto status` → `auto_approve: on (paper); paper=on live=off`
3. Off at any time: `.venv/bin/arc approve auto off`, or `!halt` to stop all trading.

Live: `arc approve auto on --env live` only *stages* the change and prints a one-time code;
re-run within 10 min with `--confirm-live <code>`. `off` is immediate. `ARC_AUTO_APPROVE`
(env var) sets the paper value only: a live process forces it off, whatever the env says.
Every flip is a `config_changes` row (`arc config history auto_approve.paper|live`,
revertable) and posts `Auto-approve: ON|OFF (env)` to the day thread; each day thread's
first line repeats the current state. The same keys are Slack-tunable
(`!arc config set auto_approve.paper true` → confirm code, owner only).
`auto_exit_defined_risk` (D24) is per-env the same way.

Approval event lifecycle (E6.2d): every `approval` routine event is started exactly once,
by exactly one path. `created` (the approval service writes it with the decision) →
`dispatched` (only on the D34 path: `broker.execute` claims the event, `dispatched_at` /
`dispatched_by` = its run id, *before* it spawns the Broker subprocess; the tick's event
drain never fires a dispatched event, so no ladder runs inline and the tick never waits
on one) → `consumed` (`consumed_at` / `consumed_by` = the one Broker run, keyed by the
event id, so two approvals in the same second both execute). A click approval skips
`dispatched`: the next tick's drain runs it. If the spawn fails the claim is released and
the same tick's drain runs it instead.

Stranded dispatch (E6.2e): if the spawned Broker dies before it claims its run (no
`routine_runs.event_id` row), the tick reclaims the event once `dispatched_at` is older
than `tick.dispatch_grace` (`config/routines.yaml`, default 10m): log
`routines.event_reclaimed`, `reclaimed N stranded event(s)` in the tick report (and
`reclaimed` in `--json` / the tick heartbeat). The drain then handles it normally: runs the
Broker, defers it while halted, or, past the proposal TTL, consumes it with a skipped
run, an `order:refused` journal row "approval lapsed: the Broker never started" and the
card edited to "not executed". `arc health check` also reports each such event once
(`stranded_events`, key `stranded:<event id>`), which matters when the tick itself is not
running. `arc routines events [--json]` lists every dispatched, unconsumed event with its
age, its run (if one started) and `STRANDED` when it is past the grace with no run.

Card post failed (E6.1b): the approval request is committed before its card is posted, so
a Slack error (`approvals.post_failed`) leaves a `pending` request with
`channel = 'post_failed'` and no `message_ts`. Every later tick's sweep (never the
in-chain `broker.execute` publish, and only once the request is a minute old) re-renders the
card for the same proposal hash and posts it once (`approvals.reposted`); a retry that
fails again is one `approvals.repost_failed` line per sweep. The tick heartbeat detail
carries `approvals: {sweep_failed, post_failed, reposted}` and the Broker reconcile card's Ops
section shows the day's sums (`Approvals: sweep failed 0 · card posts failed 1 ·
re-posted 1 · unposted 0`, hidden when all are zero). `arc health check` raises the
`approvals_unposted` condition when a pending request older than one tick still has no
card, and resolves it once every pending request has one (or expired). D34 auto-approval
is unaffected: it is decided at publish time whether or not the card posted, and a
decided request is never re-posted. Dry-run / `--no-slack` cards go to `channel = 'log'`
on purpose and are neither retried nor alerted.

Halted: an approval that arrives (or is dispatched) while halted stays pending; the drain
reports it `deferred` ("held until !resume or HH:MM ET") every tick. After `!resume`
inside the proposal's TTL (`proposals.expires_at`) the Broker runs; past the TTL the
event is consumed with a skipped broker run, a journal row (`order` / `order:refused`,
"approval lapsed under halt") and the card edited to "not executed". Nothing is sent.

Trace: `arc context trace <chain_run_id>` prints an `event` line on the `broker.execute` step
(role `dispatched`) and on the Broker step (role `ran_for`) with
`created=… dispatched=… by=<broker.execute run> consumed=… by=<broker run>`; `--json` has the
same under `events`. `sqlite3 data/arc.db "select id, dispatched_by, consumed_by from
routine_events where consumed_at is null"` lists what is still waiting.

#### Scorecard gate in front of auto-approve (E7.5a)

With `auto_approve` on, an **open** is only auto-approved when the E7.3 scorecard
(`arc/journal/scorecard.py`, `auto_approve_readiness`) shows, at approval time:

| Key (`!arc config set …` / `arc config set …`) | Default | Bounds | Meaning |
|---|---|---|---|
| `auto_approve.scorecard_gate` | `on` | on/off; off needs a confirm | off = explicit opt-out (paper as pure calibration) |
| `auto_approve.min_closed_trades` | 30 | 10–500 (never below 10) | closed trades required; also the window for the next two |
| `auto_approve.slippage_tolerance` | 1.5 | 0.5–3.0 (never above 3.0) | realised entry slippage ≤ modelled half-spread × this |

and realised net EV ≥ 0: mean realised P&L of the latest `min_closed_trades` closed
positions after entry fees (and close fees when closed by an order). Slippage is fill −
mid at entry over the same window's fills; the half-spread is the spread the proposal was
priced with (quote when valid, else the cost-model estimate). No fill with a spread →
`slippage_unknown` (fails closed). Env vars: `ARC_AUTO_APPROVE_SCORECARD_GATE`,
`ARC_AUTO_APPROVE_MIN_CLOSED_TRADES`, `ARC_AUTO_APPROVE_SLIPPAGE_TOLERANCE`.

When a criterion fails, the card is posted **with** Approve/Reject buttons and a line
`Auto-approve held back (scorecard gate): <reason>. Approve manually.`, the request stays
pending (click or `!approve` as usual; it expires on the normal TTL), and the journal gets a
`system` / `approval` / `noted` row with `reason_code=auto_approve_gated`; its payload has
`failing` (`min_closed_trades`, `negative_realised_ev`, `slippage_over_tolerance`,
`slippage_unknown`) and every number. The chain's `broker.execute` step reports
`scorecard gate held N back` and the tick summary counts them. Closes (exits) are never
gated: they reduce risk.

With `auto_approve.scorecard_gate` off, every auto-approval logs a
`approvals.auto_approve_scorecard_gate_off` **warning** and the journal reason says
`scorecard gate off`. The AUTO_APPROVE journal row (gate on or off, E6.6a) carries what
the gate said: `scorecard_gate` (`off` / `met`), `failing`, `closed_trades`,
`realised_net_ev`, `realised_slippage`, `half_spread` (`arc journal explain <hash>`), and
the card reads `Auto-approved (paper, gate off)`. `arc approve auto status`, the weekly
scorecard header and the tower Ops page print the same line:

    auto_approve: on (paper); paper=on live=off
    scorecard gate: OFF (opt-out) — 12 closed trades < 30 required; realised net EV -$8.10/trade over 12 trades

A new paper account therefore runs click-to-approve until 30 trades have closed. To
calibrate on paper anyway: `.venv/bin/arc config set auto_approve.scorecard_gate off
--reason "paper calibration"` (then `arc config confirm <code>` with the printed code).
The opt-out ends by itself (PLAN D34 paper exception): the first approvals sweep that
finds ≥ `auto_approve.min_closed_trades` closed trades sets the gate back on (change
log actor `arc:scorecard-gate`, reason `E7.5a: collection phase complete (n=30)`) and
posts `Scorecard gate: ON (auto, …)` to the day thread. It does this once; turning the
gate off again afterwards is respected.

### 5.11 Open universe (E5.7, D9/D28) and tiers (E12.1, D51)

The trade universe is four tiers resolved into one **active list** (max
`universe_active_max` 50): core (`universe`, 25 names in `config/universe.yaml`
`core:`) > momentum (E12.2) > trending (E12.3) > today's Sweep discoveries. A name
keeps its highest tier; names past the cap are journaled `universe:over_active_cap`.
SPY/QQQ are the `market_reference`: they always get a Director `regime` entry but are
not trade names. The active list resolves at 05:30 ET each trading day (`symbols`
job, context `active_universe`) and at the start of every Sweep run; ingest, EDGAR,
briefs, `ex_dividend`, Finnhub scope, monitoring and `arc history`
read it (core until the first resolve of the day).

- See it: `arc universe tiers [--json] [--db PATH] [--now ISO]` (read-only).
- **Momentum tier (E12.2):** job `universe.momentum`, 06:00 ET on the first trading
  session of each month (`days: month_start`). It writes the top 25 holdings of Invesco
  SPMO (the S&P 500 Momentum proxy) from stockanalysis.com, falling back to Schwab's
  first 20 rows (`partial`). GOOG folds into GOOGL; ETFs/funds and non-optionable names
  are dropped. The entry lives 35 days, so a failed month keeps last month's list; if
  no entry was written since the month-start slot, the job retries every trading
  session at 06:00 (`catch_up:`) until one run succeeds. The diff posts as a notice
  (`Momentum tier: +LITE +GS −NEM · 25 names · as of Oct 2`). A page as-of older than 40
  days raises `coverage:universe.momentum`. By hand: `arc universe momentum --dry-run`
  (fetch + print, no write) or `arc universe momentum [--db PATH] [--no-slack]` (runs the
  job). Weekly instead: `days: [mon]` and `context: {ttl: 8d}` in `config/routines.yaml`.
  stockanalysis lists only 25 rows, so the GOOG fold leaves 24 names (marked `partial`).
- **Trending tier (E12.3):** job `universe.trending`, 08:45 ET every trading day (TTL 1
  session: a failed run writes nothing, and the tier is empty that day, never carried
  stale). No LLM, and Alpaca is never a ranking input. Four equally weighted inputs under
  `trending.inputs` in `config/routines.yaml`: news flow (distinct `raw_docs` sources over
  3 sessions; EDGAR counts once, for the filer), Reddit (ApeWisdom pages 1–2: mentions +
  24 h rank gain), Stocktwits trending (crypto/non-US dropped) and Sweep corroboration
  (`candidates` over 3 sessions). Each input is rank-normalised to (0, 1];
  `trend_score` = sum / number of enabled inputs (an input with no data scores 0, and
  the others are not renormalised). A name needs ≥ 2 inputs; core, momentum and
  SPY/QQQ are excluded first; the top 40 get the relaxed screen and the first 25 passes
  are the tier. Every admission / screen fail / single-input reject is journaled
  (`universe:trending_*`). Notice: `Trending tier: 22 names (+NKE −LULU) · inputs news,
  reddit, stocktwits, sweep`. By hand: `arc universe trending --dry-run [--no-screen]
  [--json]` (read-only score table) or `arc universe trending [--db PATH] [--no-slack]`.
  Adding/removing an input of a known type (`news | apewisdom | stocktwits | sweep`) is a
  YAML edit.
- **Ticker extraction (E12.3):** bare upper-case words of 2–3 letters count only for
  core + momentum names (`extraction.bare_min_len: 4`); `$SYM`, `(SYM)`, `(NYSE: SYM)`
  and `ticker symbol SYM` still match any length. RSI, ET, SA, TD, MSCI, COLA, NOW… are
  stop words in bare form (`config/universe.yaml`).
- A `universe` override longer than 30 names (pre-D51 flat list) is ignored in favour
  of the yaml core (logged `universe.core_override_ignored`). Reset it from Slack with
  `!arc config universe <core 20>` so the Tower shows the core.

In seed mode (`ARC_UNIVERSE_MODE=seed`, the default) core + momentum names are always
accepted, unscreened and below the Sweep confidence floor too (journaled
`sweep_candidate` with `confidence_floor_skipped: tier=<tier>`). Trending names and
discoveries reach the Sweep only if they are in the symbol master and pass their
tier's liquidity screen profile in `config/universe.yaml` (E12.4): `strict` (price ≥ 10,
ADV ≥ 1M, near-ATM OI ≥ 500, ATM spread ≤ 10%) or `relaxed` (price ≥ 5, ADV ≥ 500k,
near-ATM OI ≥ 150, ATM spread ≤ 20%), picked by `tiers.trending.screen` /
`tiers.discovery.screen` (both `relaxed`). `ARC_UNIVERSE_MODE=strict` makes the active
list the allow-list.

1. First install (once, before the first seed-mode Sweep run; needs the paper keys):
   `set -a; source ~/.hermes/.env; set +a; arc universe refresh`
   (about 10k symbols; writes `data/symbol_master.json`). After that the `symbols`
   routine refetches it on Mondays 05:30 ET (or when missing/stale).
2. Check it: `arc universe status` (exit 1 = missing cache; ingest then uses the seed
   list only and the Sweep rejects non-seed names as `unknown_symbol`).
3. Would a name be admitted? `arc universe check PLTR HOOD` (read-only market data;
   tier membership from `--db`, default `data/arc.db`, read-only; `--fixture` =
   offline). `--profile relaxed|strict` screens every name with that profile, core
   and momentum included (a what-if; exit code = the screen result). The same checks
   run in the Sweep; rejects are journalled as `universe:<reason>` and shown on the
   Sweep card.
4. Knobs: `sweep_max_new_tickers` (discoveries per Sweep run, default 25, ceiling 25;
   trending names don't count), `universe_screen_relaxed_min_price` /
   `_min_adv_shares` / `_min_atm_open_interest` / `_max_atm_spread_pct` (Slack-tunable;
   looser is riskier and needs the confirm), `universe_mode`, `pipeline_max_shortlist`
   (the Quant/Risk budget, not a Director cap).

### 5.11 Source fairness + options data (E4.5, D30; categories + freshness E4.7, D47; six categories E4.9, D49)

Every Sweep source is a named entry in `config/routines.yaml`: each RSS feed under
`sources.rss.feeds` (`name`, `url`, optional `label`, `category`, `weight`,
`max_age`, `max_docs_per_run`, `hosts`), and `edgar` / `earnings` with a
`category` and `label`. Every context-writing source **must** declare exactly one of
the 6 categories or `reference: true` (D56); neither, both, or an unknown category
fails config load. Adding or re-weighting a source is a YAML edit only.

| category | label | `max_age` | sources | read by |
|---|---|---|---|---|
| `market_news` | Market news | 6h | WSJ Markets, CNBC Business, Nasdaq RSS, Fed RSS (D56) | Scalp, Research |
| `company_data` | Company data | 12h | Seeking Alpha, CNBC Earnings, WSJ Business, EDGAR | Scalp, Research |
| `options_fast` | Options fast | 30m | none yet (E13.6 adds the 30-min RTH source) | Scalp (typed context) |
| `options_slow` | Options slow | 24h | `vol_term`, `put_call` (`feed: scout`) | Scout, Research (typed context) |
| `youtube_macro` | YouTube macro | 24h | FX Evolution, Bravos Research | Scout, Research (channel briefs, §5.22) |
| `youtube_micro` | YouTube micro | 24h | StockedUp, Trade Brigade, Arete Trading | Scout, Research (channel briefs, §5.22) |

**Reference data (D56)** is not a category: `ex_dividend`, `macro_calendar`, the
`earnings` calendar, the `finnhub.*` kinds and `iv.record` (`iv_daily`). Those jobs
declare `reference: true`, have no weight, are never Scalp-read, keep their own
`context_ttl`, and feed the Risk step (`steps.risk.reads`), the gate's earnings
blackout (`next_earnings()` on `raw_docs source='earnings'`) and the regime step
unchanged. The Tower Sources page lists them under one *Reference data* group after
the six categories. `unusual_options` was removed (D56): stored rows expire by TTL,
and `arc context show --kind unusual_options` still lists them until then.

- **Renames (D49, D56).** `company` → `company_data` and `options_data` →
  `options_slow`; those and the pre-D47 names `company_news`, `filings`, `calendar`
  still load for one release (logged `sources.category_alias old= new=`).
  `macro_data` / `macro` were removed (D56) and fail config load with a pointer: the
  Fed feed is `market_news`, `macro_calendar` is reference data. A change-log
  override on `categories.macro_data.*`, `categories.options_data.*` or `uoa_*` is
  logged `config.override_orphaned` and ignored. `video` was split in two, so
  `category: video` fails config load with a pointer: set `category: youtube_macro |
  youtube_micro` on each `youtube.briefs` channel. Stored rows that still say
  `company` / `macro` / `video` are read through `normalize_category` (a `video`
  brief resolves by its channel slug; a stored `macro_data` story has no category).
  A change-log override on an old key
  (`categories.company.weight`) applies to the renamed key; one on
  `categories.video.*` is dropped with `control.override_unknown_key`.
- **Categories.** The top-level `categories:` block sets each category's `weight`
  (all 1 = equal) and freshness `max_age` (table above). Both are Slack-tunable:
  `!arc config set categories.<c>.weight 0-5` and `categories.<c>.max_age <minutes>`
  (30-10080, every category). A source's `weight` is its
  share *inside* its category, so a 4th market_news feed takes a quarter of
  market_news, and other categories don't move. YouTube channels split their own
  category the same way (2 macro channels = 1/2 each, 3 micro = 1/3 each).
- **Budget.** Each Scalp run reads `scalp_doc_budget` docs (default 120, Slack-tunable
  20-400), split equally across its two doc categories (`market_news`,
  `company_data`; D56 `funnel.scalp.doc_budget_split: equal`) that have fresh docs, then
  by source weight inside each category (weighted round-robin; a category or source
  with nothing left gives its share to the others). Newest first within a source.
  Docs over budget wait for the next run.
- **Freshness.** A doc older than its category's `max_age` (published time;
  `earnings` uses ingested time, `age_basis: ingested`) is never read: the Sweep
  closes it `raw_docs.scalp_status='skipped_stale'` with the run id, and the `rss` /
  `edgar` connectors don't store it at all (`ingest.skipped_stale count= source=`).
  Docs still inside their window but past the `raw_doc_ref` TTL (5d) are closed
  `skipped_budget`. Never deleted. `story` and `candidate` entries expire at
  min(1 session, freshest source's `max_age` + 2h).
- **Title filters (D55, E4.11).** An RSS feed may set `title_exclude` / `title_include`
  regex lists (case-insensitive, on the entry title). A filtered entry is stored
  already closed `raw_docs.scalp_status='filtered'` and never read; the next Sweep run
  claims it (`sweep_run_id`) and counts it once (`filtered` metric, a *Filtered* line on
  the Sweep card); the `rss` run reports `new_<feed>` / `filtered_<feed>` metrics and the
  Tower Sources page shows `N filtered (title)` per feed today. Seeking Alpha ships with
  three patterns for fund/ETF dividend declarations (~31% of its docs). The lists are
  edited in `config/routines.yaml` by PR only (not `!arc config`). The retired `cnbc`
  key (CNBC Economy, replaced by `cnbc_earnings` + `cnbc_business`) keeps its label
  for old rows for one release.
- **Source mix.** The Sweep card groups it by category: `*Market news* 50% · 6
  read: WSJ 1 · Nasdaq 5 (10 over budget)`, with `(N stale)` per source.
- **Director.** Its prompt carries a code-built *Context by category* block: the 6
  headers in fixed order, each with a freshness line (`Market news: 14 stories,
  newest 22m`, `Options slow: vol_term 5h, put_call 8h`, `YouTube macro: 1/2
  channels (missing: Bravos)`); an empty category reads `no fresh info`. Reference
  data is never listed there.
- **Typed-kind freshness (D49).** The category `max_age` also applies to typed
  context (vol_term, put_call, channel_brief), measured from `valid_from`. An older
  entry is listed as `stale (age)`, e.g. `Options slow: no fresh info (vol_term stale
  (13h))`; a category with nothing fresh reads `no fresh info`. The context TTL is
  unchanged, so stale entries stay readable for audit and the Tower. A Director call
  recorded before D49 (no `categories` input) replays with the old 5-category block,
  and one recorded under D49 (`macro_data` / `options_data` in its `categories`
  input) with the D49 six-category block and unusual options, byte for byte
  (`arc journal replay`).
- **EDGAR.** `published_at` is the filing's `acceptanceDateTime` (filing date if
  absent). Filings older than the company window are skipped before download, and
  the cursor is the newest accession seen, so a filing re-listed on the feed (or the
  backlog of a newly added ticker) is never ingested.
- **Stories.** Near-duplicate headlines (Jaccard ≥ `sweep_story_threshold` within
  `sweep_story_window_hours`) are one story; EDGAR filings group by filer + form.
  Corroboration on a candidate = distinct *sources* behind its URLs, computed in code
  (`candidates.corroboration`); the LLM cannot raise it.
- **Two stages.** Stage 1 digests stories in batches of `sweep_batch_size` on the
  Sweep's (cheap) model; evidence quotes must appear verbatim in a doc, else dropped;
  a failed batch falls back to an extractive digest (headline + lead). Stage 2 reads
  the digests, `sweep_story_batch_size` (40) per Sweep call. Both stages'
  tokens/cost land in `scalp_batches`
  (`stage` = `digest` | `sweep`) and the run manifest.
- **Options and reference data** (free, no key; typed context kinds):

  | Job | Source | Kind | Category | When (ET) |
  |---|---|---|---|---|
  | `vol_term` | Cboe VIX9D/VIX/VIX3M/VVIX daily history | `vol_term` (contango/backwardation) | `options_slow` | 09:00, 16:45 |
  | `put_call` | Cboe daily market statistics | `put_call` | `options_slow` | 09:00 |
  | `macro_calendar` | federalreserve.gov FOMC page + BLS release ICS + BEA release ICS | `macro_calendar` | reference | 05:45 |
  | `ex_dividend` | Alpaca corporate actions | `ex_dividend` per ticker | reference | 06:15 |

  BLS rejects a User-Agent without a contact email (403): it is sent
  `ARC_EDGAR_USER_AGENT`, same as EDGAR. The self-computed unusual options detector
  (`unusual_options`, `uoa_*` settings) was removed in D56; `options_volume_daily`
  keeps its history rows but nothing writes them.

### 5.12 Close quote check (E6.2a) and the live execution test

Every close (monitor exits, Quant exits, the close leg of a swap) is priced
from quotes that must pass `close_quote_sanity` first; otherwise nothing is
proposed, no gate token is minted and no order is sent. The journal gets an
`exit:quote_unusable` row with every leg's bid/ask/sizes/quote time/spread, and
the next tick re-prices. The `close.quotes` log line carries the same evidence
on every close attempt (pass or fail), so a missed close can be diagnosed from
the logs.

| Check | Knob (`!arc config execution`) | Default |
|---|---|---|
| each leg's quote age, from the quote's own timestamp | `close_quote.max_age_seconds` | 60 s |
| gap between the legs' quote timestamps | `close_quote.max_skew_seconds` | 30 s |
| each leg's spread (passes if under either cap) | `close_quote.max_spread_pct` / `close_quote.max_spread_abs` | 10% / $0.10 |
| combo mid vs the same combo read off the expiry's strike curve | `close_quote.max_curve_dev` | $0.15/share |
| owner alert in #arc-investor after N failures in a row (defined risk) | `close_quote.alert_after` | 6 (30 min at the 5-min monitor) |

Why the curve check: the free `indicative` options feed (D7) is derived, not
OPRA. Its per-strike quotes are fresh and tight on every read but jitter by
$0.10–0.30 from read to read, so a vertical priced from one read can sit
entirely outside the market (the 2026-09-29 cleanup miss). The neighbouring
strikes smooth that out; a read that disagrees with them is skipped.

**Live execution test.** `tests/test_integration_execution.py` opens and closes a
real 1-lot SPY paper vertical **on the dedicated test account** (below). It is
opt-in, so `make check` and ordinary full suites never trade:

```
set -a; source ~/.hermes/.env; set +a
ARC_LIVE_EXEC_TESTS=1 .venv/bin/pytest tests/test_integration_execution.py -rA
```

It needs the test account's keys, `ARC_GATE_SECRET` and RTH (off-hours it skips; a skip is
not a pass). It skips without trading if the close's quotes are unusable before
the open. Cleanup retries the close once from fresh quotes; if that also fails
the test fails and names the legs to close by hand. Each run sends up to ~12
orders. Run one at a time: two concurrent runs
trade against each other (wash-trade rejects) and leave spreads open.

**Two paper accounts (E6.2c).** Integration tests never trade the production
paper account. On 2026-09-28/29 RTH test runs left 8 and 21 fills there with no
local record; the 16:30 reconcile halted production (`fill_unknown`) and the
next 09:30 chain was skipped both days.

| Account | Keys (`~/.hermes/.env`) | Used by |
|---|---|---|
| production paper | `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | every `arc` command, the tick, the tower |
| test paper | `ALPACA_TEST_API_KEY` / `ALPACA_TEST_SECRET_KEY` | `tests/test_integration_alpaca.py`, `tests/test_integration_execution.py`, `.github/workflows/integration.yml` only |

- Nothing under `arc/` reads the `TEST_` keys (a unit test enforces it). The tests
  build the adapters with explicit keys (`AlpacaPaperBroker(api_key=…, secret_key=…)`).
- Without the `TEST_` keys the integration tests **skip**. If `ALPACA_TEST_API_KEY`
  equals `ALPACA_API_KEY` they **fail**: that is the accident this rule prevents.
- Every test order's `client_order_id` starts with `test.` (the tests' broker
  wrapper adds it; `arc.execution.submit()` never does). If one still lands on
  the production account, reconcile reports it as a `fill_test` notice (journal
  `reconcile:test_fill`, info line on the Broker reconcile card) and does **not** halt. A
  leg such an order left open is still `position_unattributed` and halts.
- The D32 order budget counts orders per account, so test runs no longer use
  the production account's daily budget.

Owner setup (once):

1. In the Alpaca dashboard (paper), open the account menu → *Open new paper
   account* (or use a second Alpaca login). Enable options trading (level 3) on it.
2. On that account, *API Keys* → *Generate*. Copy the key id and secret.
3. Add to `~/.hermes/.env` (never commit it):
   `ALPACA_TEST_API_KEY=<key id>` and `ALPACA_TEST_SECRET_KEY=<secret>`.
4. Add the same pair as repo secrets for the weekly workflow:
   `gh secret set ALPACA_TEST_API_KEY -R mohitgulla/Project-Arc` and
   `gh secret set ALPACA_TEST_SECRET_KEY -R mohitgulla/Project-Arc`. The old
   `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` repo secrets are no longer read and can be deleted.
5. Check: `set -a; source ~/.hermes/.env; set +a; .venv/bin/pytest tests/test_integration_alpaca.py -rA`
   passes (not skipped), and the `account_id` the test sees is not the production one.

Close strays and resume after a reconcile halt:

1. See what reconcile found without raising another halt:
   `.venv/bin/arc reconcile --no-halt` (JSON: `mismatches`, `notices`).
2. List the day's orders with their client ids:
   `.venv/bin/python ~/.hermes/skills/software-development/project-arc-development/scripts/paper_account_audit.py --after <YYYY-MM-DD>T13:30:00Z`.
   `test.`/`arc2.` ids without a local record and legs no structure accounts
   for are the strays.
3. Close stray legs in the Alpaca dashboard (paper → Positions → Close), or
   cancel stray open orders there. Don't trade them closed through Arc.
4. Re-run `.venv/bin/arc reconcile --no-halt`. `position_*` mismatches must be
   gone. Today's untagged stray fills keep showing as `fill_unknown` for the
   rest of the ET day even after you close them (the fills happened); that is
   expected once every one is accounted for in step 2. `test.` fills appear only
   under `notices` and never block.
5. Resume: `!resume` in Slack, or `.venv/bin/arc resume --actor <owner Slack id>`.
   Check with `.venv/bin/arc halt-status` (exit 0 = not halted).

### 5.13 Portfolio-aware Director, idea dedupe, no-trade (E5.9, D33)

Before every entry chain the Director step runs three deterministic pieces
(no LLM, audit DB only), in this order:

1. **Market guard** (`arc/pipeline/market_guard.py`). VIX comes from the fresh
   `vol_term` context (E4.5), else `PipelineEnv.vix_quote` (fixtures:
   `arc/pipeline/fixtures/vix.json`). New opens are blocked, and the chain stops
   before any LLM call, when VIX ≥ `no_trade.vix_max`, the term structure is in
   backwardation (`no_trade.on_backwardation`), or SPY's regime stickiness is
   under `no_trade.transitional_min_confidence`. No VIX at all fails closed
   (`market_data_missing`) unless `no_trade.require_vix` is off. Exits never go
   through the guard: the positions chain keeps closing.
2. **Portfolio context** (`arc/pipeline/portfolio_context.py`, kind
   `portfolio_context`, subject `session`, 10 min TTL). Account, every open
   structure with its mark P&L, max loss, Greeks, sector
   (`config/sectors.yaml`; unmapped → `unknown`), expiry bucket and original
   thesis, plus aggregates (allocation by underlying/sector/stance/expiry, HHI,
   Greeks vs caps, flags, underlyings at the 5% cap). With an empty book the
   prompt is E5.7's plus one line; otherwise the rendered block (capped at
   `portfolio.context_max_positions`) is added and the Director must return a
   `portfolio_view`, a `portfolio_fit` per pick and `thesis_checks` per holding.
   Those go to `note` context (`portfolio_view`, `thesis_check` topics) and the
   journal; they never reach the gate.
3. **Idea dedupe** (`arc/pipeline/dedupe.py`). Fingerprint = ticker, stance,
   structure type, expiry ISO week, short-strike bucket (1% of spot). An idea
   is held while its fingerprint was executed (open, or closed within
   `dedupe.executed_cooldown` sessions), proposed (`dedupe.proposed_cooldown`)
   or owner-rejected (`dedupe.rejected_cooldown`). Windows double in the D32
   restrictive tier. A closed/proposed/rejected prior is re-admitted when spot
   moved ≥ `dedupe.reprice_move_pct` or the regime changed
   (`dedupe_override` journal row); an open structure is never overridden. The
   Director stage holds `ticker|stance` prefixes (listed to the Director as
   "recently suggested or held"); the propose stage checks the full fingerprint
   on the live spot. Migration 016 replaces the one-open-per-ticker-per-day
   index with `(chain_run_id, ticker)`; manual `arc propose` runs use
   `manual-<day>-<run_id>`.

An empty shortlist is an explicit outcome: the Director's `no_trade_reason`
(`no_fit | too_volatile | unclear | budget | portfolio_full`) is journalled as
`director_no_trade`, shown on the card as `[Director] No trade: …`, and the
chain stops (`stop_chain` in the run's metrics; no Quant/Risk calls).

Deterministic Director-stage drops, journalled per ticker: `dedupe_executed`
(the ticker is held in that stance), `drop_at_cap` (underlying at its max-loss
cap), `drop_concentration` (the Director says `adds_concentration` on a sector,
stance or underlying that is already flagged).

Knobs live under `!arc config dedupe | portfolio | no_trade`.

### 5.14 Two-speed routines: 30-min Scalp, 10-min trading loop (E5.8, D31/D36/D52; Scalp was Sweep until D56)

**What runs (ET, trading days).** `config/routines.yaml` declares it; nothing
fires without the `arc routines tick` cron (E5.3).

| job | cadence | what |
|---|---|---|
| `rss`, `edgar` | every 15m 06:00-20:00 | sources, at least as often as the Sweep |
| `sweep` | every 30m 09:00-16:00 (15 runs) | candidates from the last 30 min of sources; ttl 20m, no catch-up |
| `sweep.overnight` | 22:00 daily | the after-close pass over fast sources; ttl 3h (YouTube moved to `youtube.briefs`, §5.22) |
| `director` | every 10m 09:40-15:50 (38 slots, D52) | the trading loop: director → quant → risk → propose → execute; ttl 5m |
| `monitor` | every 10m 09:30-16:00 (40 slots, D52) | positions, Greeks, marks (D35) |

The `sweep.completed → director` trigger is gone: the loop picks a new candidate
up within one slot.

**Loop rules (deterministic, in the dispatcher / Director, not the LLM):**

- *No overlap.* A loop slot that finds the previous loop (or a Sweep holding the
  LLM lock) still running is recorded as `skipped` and never caught up. In one
  tick the loop runs before the Sweep (name order), so a Sweep can't starve it.
- *Deadline.* `loop.max_runtime` (4m). A step already running may finish; no
  later step starts (`timeout: loop exceeded 4m`). One Slack notice per day.
- *Change-aware.* The Director digests its inputs (candidate ids, regime entries,
  positions, day-P&L bucket of `loop.pnl_bucket_pct` % equity, pending orders,
  budget tier, suppressed ideas). Same digest as the last full run and less than
  `loop.max_idle` (30m) since it → `no_change`: Director/Quant/Risk/Propose are
  skipped (no LLM call), `broker.execute` still runs (`on_no_change: run` in `steps:`)
  so pending approvals and ladders carry on. Journal row `loop_no_change`; the
  run manifest carries `loop_inputs` (digest). A manual `arc propose` never skips.
  Only a Director run that *completed* its evaluation advances the loop's
  `last_full_run`: a failed LLM call leaves the previous one in place, so the
  next slot evaluates in full again instead of being muted as `no_change`.

**Slack (D36).** Every loop slot posts one root line in #arc-investor:

```
✅ 2026-09-28 09:40ET • Portfolio: $101,234 • P&L: +$312 • Orders: 3/200 • BUY: SPY
⏳ … • PENDING: SPY          (card awaiting the owner)  /  WORKING: SPY (ladder running)
✖ … • HOLD | HOLD (no change) | HOLD (timeout) | HOLD (skipped: previous loop running)
```

The thread under it, in order: `[Sweep] Context: N Candidates • run <stamp>`
(the candidate entries the Director read, with the Sweep run that wrote them),
`[Director]`, `[Quant]`, `[Risk]`, the proposal card, `[Broker]` when a ladder
ran, and last a `[Routines] <chain> director=12ms … digest=…` code block. A
`no_change` loop gets only the `[Routines]` reply. The root is re-rendered from the DB
on approve / reject / expire and after the Broker's fill. `loop.post_hold_roots`
off drops the roots of skipped slots; `loop.slack_layout = day_thread` is the
rollback to the single day thread (cards and heartbeats as before D36).

**Knobs** (`!arc config loop`): `loop.max_idle` (5-240 m, riskier up),
`loop.max_runtime` (1-5 m, hard ceiling 5), `loop.pnl_bucket_pct`,
`loop.post_hold_roots`, `loop.slack_layout`. Cadences: `routines.sweep.cadence`,
`routines.director.cadence` as before.

**Check it:**

```
arc routines validate
arc routines tick --dry-run --since 2026-10-01T00:00-04:00 --now 2026-10-01T23:59-04:00 --step 5m --no-slack
#  planned: director ×75, sweep ×15, sweep.overnight ×1, monitor ×79, rss ×57 …
sqlite3 data/arc.db "select key, value from routine_state where key like 'loop%' order by key"
```

### 5.15 Asking why: journal explain / attribution / counterfactual (E9.3)

Three read-only views of the audit store answer "why was this decision made,
was it good in hindsight, what would have been better". They open the DB with
`mode=ro` + `PRAGMA query_only` (never create, migrate or write it; a missing
`--db` path exits 2 instead of creating an empty store). Point `--db` at a copy
(`sqlite3 data/arc.db ".backup /tmp/arc-copy.db"`) when you want to be sure.
Every command prints a short text summary; `--json` prints the full typed,
versioned document (what the Analyst's pre-run script E9.2 and the future D26
`!arc explain` read).

**Why was this decision made?** One document per proposal: the journal
decisions, every persona call of its chain (prompt sha256 + raw reply, model,
tokens), the gate verdict and violations (`token_present` only, the token is
never printed), the approval request + `approvals` row, the order/fill timeline
(`orders`, `order_events`, `fills`), the local position, the outcome row
(`realised_pnl`, `pnl_vs_ev`, `slippage_bps`, `hold_to_expiry_shadow_pnl`,
`exit_reason`) and any reviews. Proposals that never traded (open, rejected,
expired, gate-failed) get the same document with `outcome: null`.

```
arc journal explain de7880f035d2 --json          # proposal hash or unique prefix
arc journal explain run-5361867e7d304272 --json  # the propose run → its chain's proposals
arc journal explain chain-1e583f91cdda           # + chain decisions with no proposal
```

`status` is derived, most advanced fact first: outcome status → position
open/closed → `execution_<status>` → approval status → `gate_failed` → `proposed`.

**Did it pay, and where?** Realised P&L attribution over closed trades
(positions closed in the window, plus `outcomes` rows with no local position):

```
arc scorecard attribution --since 2026-09-01 --by kind,regime,persona_model --json
#  buckets: [{key: {kind: iron_condor, regime: risk_on, persona_model: director=…,quant=…,risk=…},
#             n, realised_pnl, win_rate, avg_pnl_vs_ev, slippage_realised_usd,
#             slippage_modelled_usd, n_slippage, low_sample: true}]
```

Dimensions: `kind` (structure), `regime` (Director regime at proposal time),
`persona_model` (the `persona=model` set of the chain's successful LLM calls),
`ticker`. Buckets with `n < 30` carry `low_sample: true`; the Analyst's
MIN-SAMPLE gate reads that flag rather than re-deriving it. Slippage is entry
slippage (fill − mid) summed over trades that have a modelled value, next to
the modelled `entry_slippage`. `arc journal scorecard` is still the weekly report.

**What would have been better?**

```
arc journal counterfactual --since 2026-09-01 --json
```

- `closed`: each closed trade's realised P&L next to the D19 hold-to-expiry
  shadow (`pending` until the legs expire and a settlement is known) and the
  no-trade alternative ($0).
- `not_traded`: every rejected / expired / not-actionable / gate-failed open
  proposal, marked as if filled at its limit at the latest cached E7.1 EOD
  session (`--data-dir`, default `data/`; `n/a` when the history isn't cached).
- `alternatives`: Quant menu alternatives vs the chosen structure (per contract).

The `not_traded` and `alternatives` rows come from the same code as
`arc journal gaps` (which now prints a "proposals not traded" section too), so
the two never disagree.

### 5.16 Arc Analyst: weekly strategy review (E9.2)

An independent reviewer of the *strategy* (the Sentinel, D23, reviews the code).
Boundary: does the fix change a decision the system makes → Analyst; does it
change whether the system does what the spec says → Sentinel. The Sentinel's
lens 7 (strategy) is info-only: it cross-references the Analyst and never files
strategy cards (skill versioned in `hermes/sentinel/`, installed by
`hermes/sentinel/install.sh`; E9.4). The Analyst's artefacts are
versioned in `hermes/analyst/` and copied into its own Hermes profile
`arc-analyst` by `hermes/analyst/install.sh` (E9.4 does the profile and the start):

| Repo file | Installed as |
|---|---|
| `hermes/analyst/skills/arc-analyst/SKILL.md` | `~/.hermes/profiles/arc-analyst/skills/arc-analyst/SKILL.md` |
| `hermes/analyst/arc_analyst.py` | `~/.hermes/profiles/arc-analyst/scripts/arc_analyst.py` (cron pre-run gate) |
| `hermes/analyst/prompt.md` | the cron prompt |

The cron `arc-analyst-weekly-audit` runs Sunday 14:00 PT (after the weekly scorecard)
on `anthropic/claude-fable-5.1` at `max` effort and delivers to #arc-analyst.
`install.sh` creates it **paused**; `install.sh --dry-run` prints every step.

The pre-run gate needs no secret (no Alpaca key, no `ARC_GATE_SECRET`, no Slack
token; they are stripped from every command it runs). It:

1. copies `data/arc.db` with the SQLite backup API (live file opened `mode=ro`,
   only `backup()` is called on it) to `~/.hermes/profiles/arc-analyst/analyst/runs/<date>/arc-copy.db`;
2. prints `{"wakeAgent": false}` (no LLM run, nothing posted) unless the journal
   has ≥ 1 closed outcome **and** a closed outcome or a halt arrived since the
   last run;
3. otherwise runs the read-only views on the copy (`arc scorecard attribution`,
   `arc journal scorecard|gaps|counterfactual|show`, `arc config diff|history|show`),
   builds a realised-vs-model table (realised P&L vs EV, slippage bps vs the
   Quant's cost bps) by structure kind × regime, lists halts, the newest
   `docs/RESEARCH/*.md` and the A-id ledger, and prints it (≤ 30k chars).

```
python3 hermes/analyst/arc_analyst.py                         # the gate (ARC_ANALYST_DB / ARC_REPO to override)
python3 …/arc_analyst.py record RUN_DIR                       # validate findings.json → ledger (A-ids)
python3 …/arc_analyst.py check-report RUN_DIR/report.md       # ≤ 3,500 chars, all sections
python3 …/arc_analyst.py mark RUN_DIR                         # advance the watermark
python3 …/arc_analyst.py triage A-3 wontfix "why"             # owner: accepted|wontfix|fixed
python3 …/arc_analyst.py reset                                # next run reviews unconditionally
```

`record` rejects findings that break the strategy gates: a recommendation from a
bucket with fewer than 30 closed trades (MIN-SAMPLE), a recommendation that is
not one experiment with variable / values / metric / effect size / status
(ONE-VARIABLE), more than 3 recommendations, or a missing status line for any of
the six standing themes (cost model, regime menu, ranker, exit policy, sizing,
auto-approve). Obvious config flaws are allowed at any N. Owner commands in the
report thread: `create A-<n>`, `comment A-<n>`, `wontfix A-<n> <why>`,
`accept A-<n>`, `rerun`.

### 5.17 Forward A/B experiment registry (E10.1, D44)

Pre-registration only: nothing here trades (the treatment runner is E10.2).
Specs live in `config/experiments/live/<id>.yaml`; defaults (alpha 0.05, power
0.8, 20/60 sessions, A/A 10 sessions) in `config/experiments.yaml`,
tunable as `experiments.*` (`!arc config experiments`). The treatment overlay uses
the same deep-merge format as the backtest overlays in `config/experiments/*.yaml`.

    arc experiment create --spec config/experiments/live/xp1_aa_baseline.yaml   # draft
    arc experiment register XP-1        # locks sha256(canonical spec); queued if the area is busy
    arc experiment show XP-1 [--json]
    arc experiment verify XP-1          # exit 1 when the stored spec no longer matches the lock
    arc experiment list [--status running]
    arc experiment stop XP-1 --reason owner --actor local

- After `register` the spec is locked (store check plus a DB trigger): a change
  needs a new id. One `registered`/`running` experiment per area; the next
  queued one is registered when it stops.
- An `ab` experiment cannot start before an `aa` stopped with sigma recorded
  (E10.4), unless the owner overrides it (journaled `experiment:aa_override`).
- Every step writes a `decisions` row (stage `experiment`). `arm_id` on
  `run_manifests`, `proposals`, `decisions`, `outcomes`, `pnl_snapshots` and
  `executions` is NULL for control (all rows today).

### 5.18 Daily evaluation and verdict (E10.3, D44)

The `experiments.evaluate` routine (trading days 16:40 ET, after the 16:30 broker.reconcile
reconcile; deterministic, halt-exempt) evaluates every running experiment and
stores one `ExperimentReport` in the append-only `experiment_reports` table:

    arc experiment report XP-2 --db <db> [--json]      # computed now, read-only
    arc experiment report XP-2 --db <db> --stored      # latest stored report
    arc experiment evaluate [XP-2] --db <db> [--now ISO]   # what the routine does

- Series: `d_t = (treat_pnl_t − ctrl_pnl_t) / t0_equity` per session from each
  arm's EOD `pnl_snapshots`; control's legacy-book P&L (marks in control's
  `positions_snapshots` + close cash) is removed. Missing sessions are listed.
  The treatment arm is read on its **virtual** equity only
  (`details_json.virtual_equity`, = t0 equity at t0, written by the E10.2 arm
  reconcile); the experiment account's broker `equity` is never used, so a
  treatment row without `virtual_equity` counts as a missing session.
- Primary: always-valid mSPRT confidence sequence (normal mixture). σ is the
  A/A's when recorded, else the running sd inflated to its chi² upper bound
  (`experiments.stats.sigma_upper_q`). Win = lower bound > 0 after
  `min_sessions` **and** Sortino non-inferior (paired bootstrap CI vs margin).
- No guardrail (harm) auto-stops (owner, 2026-10-03): only the primary and
  secondary metrics decide. Each arm's drawdown, worst day and order count are
  in the report for the owner, who stops an experiment by hand
  (`arc experiment stop XP-2 --reason harm --actor local`).
- `max_sessions` without a win → stop(futility); for an A/A that is the normal
  end and records σ (unlocks ab starts). An A/A whose CI excludes 0 → stop(invalid).
- Breakdowns by regime / structure kind are reported, never decision inputs.

### 5.19 Arm runner (E10.2, D44)

The treatment arm is the same trading loop on its own paper account
(`ALPACA_EXP_*`) and its own store (`experiments.runner.arms` in
`config/experiments.yaml`; N arms is configuration, e.g. a paper shadow control).

    arc experiment start XP-1 --db data/arc.db           # t0 (live: arm account must be flat)
    arc experiment start XP-1 --fixtures --arm-dir <scratch> --db <scratch>.db
    arc experiment pair <control chain id> --db <db> [--fixtures --fixture-set bullish]
    arc experiment arms-tick --db data/arc.db           # what the live tick spawns

- t0: each arm store gets a one-row `arm_identity` and a virtual account opened
  at control's equity (broker equity for live, the fixture account offline);
  control's open structures are the legacy book, their max loss is reserved in
  the arm until each closes on control. The arm account must have no positions
  or open orders: close them in the Alpaca dashboard (no auto-flatten).
- The arm sizes and gates from its virtual account (`VirtualBroker`): equity
  moves only with its own fills/marks, buying power = min(broker, virtual −
  legacy), and under a cash profile only settled cash (T+1) counts. Control's
  account profile (debit-only, day-trade limit, D32 budget) applies unchanged.
- Shared inputs: Sweep/sources run once, in control. The arm reuses control's
  steps before the fork step (the first step its overlay changes; never later
  than `propose`) and replays control's market tape (`market_tape`). Every arm
  manifest carries `paired_chain_run_id`, `fork_step`, `arm_id`, `git_sha`.
- After each live control tick, `arc routines tick` spawns `arc experiment
  arms-tick` detached (own lock, own lock dirs), only while an experiment runs.
- Evaluation reads arm rows through `arc.experiments.paired.paired_view`: arm
  stores ATTACHed read-only, `arm_id` projected per store. Nothing is copied
  into the control store (D32 counts and Tower stay control-only).

### 5.20 Strategy-lane CI check: two PR lanes (E10.7, D44)

Every pull request runs the `strategy-lane`
CI job (`scripts/strategy_lane_check.py`; paths and knobs in
`config/strategy_lane.yaml`). It is deterministic: `git diff` between the merge base
and the PR head plus a YAML leaf diff, no network beyond reading the PR body, no LLM.

A PR is **strategy lane** when it touches a strategy path: `arc/exits/`,
`arc/scanner/`, `arc/sizing.py`, the pipeline selection/ranking modules
(`arc/pipeline/steps.py`, `dedupe.py`, `portfolio_context.py`), persona prompts
(`arc/personas/builders.py`, `entry_window.py`, `hermes/skills/arc-*/SKILL.md`),
`config/{exits,ranking,account_profiles,universe,costs}.yaml` and the lane config
itself. Gate and safety code (`arc/gate/`, `arc/budget/`, `market_guard.py`,
execution, reconcile, halts) is not. Such a PR passes only with one of these lines in
its body:

    Experiment: XP-<n>            # this change is what XP-<n> tests (spec in config/experiments/live/)
    Flag: <stem>.<key.path>      # a NEW key in config/<stem>.yaml, default off (false/off/none/null/control)
    Lane: fast — <reason>        # bug / safety / infra fix (>= 10 chars); arc-sentinel audits these

- **Flag** — the check confirms the key is new in this PR and its value is off, so
  both experiment arms run the same binary in control behaviour until an overlay
  turns it on. Example: `Flag: exits.pipeline.skip_iv_crush`.
- **Promotion** (flipping a default): any change or removal of an existing value in
  `exits`, `ranking`, `costs` or `account_profiles` YAML (the files an experiment
  overlay can patch). It needs `Experiment: XP-<n>` with a committed verdict file
  `config/experiments/live/verdicts/XP-<n>.yaml` (`experiment_id`, `verdict: win`,
  `report_hash` copied from `arc experiment show XP-<n> --json`), and every changed
  value must equal that experiment's treatment overlay. `Flag:` and `Lane: fast` never
  cover a promotion. Comment-only YAML edits are not promotions.
- A value change in `universe.yaml` (no overlay can test it) needs any one lane line.
- A wrong extra line (an unknown `XP-<n>`, a flag that is not new) fails even if
  another lane line passes, so the audit trail never cites something false.
- The job reads the PR body at run time: after fixing the body, re-run the
  `strategy-lane` job (`gh run rerun <id> --failed`); no new push is needed.
- Local dry run: `.venv/bin/python scripts/strategy_lane_check.py --base origin/main
  --head HEAD --body-file <body.md>` (exit 0 pass, 1 fail, 2 config error).

### 5.21 A/A calibration run XP-1: runbook for E10.8 (E10.4, D44)

XP-1 (`config/experiments/live/xp1_aa_baseline.yaml`) runs the production config on
both paper accounts (empty overlay on both arms) for `experiments.aa_sessions` (10)
sessions. It measures sigma of the paired daily difference d_t, the achievable MDE at
20/40/60 sessions, the slippage and fill-rate gap between the two accounts and the
LLM-divergence rate. alpha 0.05 and min 20 / max 60 sessions are owner policy (D44)
and are not changed by the A/A. The report template is
`docs/RESEARCH/experiments/XP-1-aa.md` (header "pending live run (E10.8)").

Dry run (scratch stores, fixtures, no broker, no orders), any time:

    .venv/bin/python scripts/xp1_aa_dry_run.py --dir <empty scratch dir> [--inject-usd 150]

`--inject-usd` gives the treatment a fake daily edge: the run must end `invalid`.

**Pre-checks (all must hold; E10.2a's RTH evidence must be done first):**

1. Experiment account flat. `arc experiment start` refuses otherwise
   (`live_flat_check`: no positions and no open orders on `ALPACA_EXP_*`). Close or
   cancel anything in the Alpaca dashboard; there is no auto-flatten.
2. Control's legacy book holds at most 2 structures:

       sqlite3 -readonly data/arc.db "select count(*) from open_structures where status='open'"

   Every open structure becomes legacy at t0: excluded from both arms, with its
   max loss reserved in the arm until it closes on control. More than 2 means
   waiting for closes.
3. Halts clear: `.venv/bin/arc halt-status` prints `trading allowed` (exit 0).
4. No experiment running in area `other`: `arc experiment list --db data/arc.db`.
5. `experiments.runner.enabled` is `true` (`!arc config experiments`) and the
   routines tick cron is installed (`hermes cron list`).

**Start (before the 09:30 ET open, so the first session is the same day):**

    arc experiment create --spec config/experiments/live/xp1_aa_baseline.yaml --db data/arc.db
    arc experiment register XP-1 --db data/arc.db
    arc experiment start XP-1 --db data/arc.db

`start` reads control's broker equity as t0 equity, creates
`data/arc-exp-XP-1.db` and opens the arm's virtual account. From the next tick on,
the tick spawns `arc experiment arms-tick` after every control loop chain.

**Reading the daily line.** After the 16:40 ET `experiments.evaluate` routine, the
#arc-investor day thread gets one line per running experiment:

    [XP-1] A/A Day 4 • P&L ∆ −0.02%/day (p 1.00) • Sortino ∆ −0.35 (p 0.62)

- `Day N`: paired sessions so far (both arms have an EOD snapshot). A session one
  arm missed is skipped and listed under `missing` in the report.
- `P&L ∆`: mean of d_t, % of t0 equity per day, with its always-valid p. On an A/A
  it should hover around 0 with a large p.
- A stop card posts when the verdict changes: `Futility` after 10 sessions is the
  normal end and records sigma (it unlocks A/B starts); `Invalid` (CI excludes 0)
  means the harness is broken: the arms differ when they should not. Investigate
  the pairing (`arm_pairs`, manifests) before any A/B.
- The full report: `arc experiment report XP-1 --db data/arc.db` (computed now) or
  `--stored` (the 16:40 one). Tower: Experiments page.

**Stop by hand** (any time, e.g. arm trouble):

    arc experiment stop XP-1 --reason owner --actor local --db data/arc.db --note "<why>"

The tick stops spawning arms-tick once no experiment is running. To pause the arms
without stopping the experiment: `!arc set experiments.runner.enabled false`.

**Write the report** after the stop (futility or invalid):

    arc experiment report XP-1 --db data/arc.db --stored --format md \
      --out docs/RESEARCH/experiments/XP-1-aa.md

and commit it (replaces the template's dry-run example).

### 5.22 YouTube channel briefs: one 02:00 ET run, five channels (E4.6, D45; categories E4.9, D49)

**What runs.** `youtube.briefs` in `config/routines.yaml`, 02:00 ET (23:00 PT the evening before) on
trading days, background lane, holds the LLM lock, `ttl: 3h` (a missed night is
skipped after 05:00, never caught up). Every channel declares its category
(`youtube_macro` or `youtube_micro`, required; the job has no `category:`).
Channels, in config order:

| slug | channel | category | notes |
|---|---|---|---|
| `stockedup` | StockedUp `UC-m6zNItyoDk5lSykDlhE4Q` | `youtube_micro` | Shorts skipped |
| `fxevolution` | FX Evolution `UCvJZEG5x-DVYZKTz--pS39w` | `youtube_macro` | `Live Stream` titles excluded |
| `tradebrigade` | Trade Brigade `UCYKtr6GfycBqQJf32tbQSbQ` | `youtube_micro` | 50-77 min videos: `max_audio_minutes: 90` |
| `arete` | Arete Trading `UCTeFsS-bP0XEt3NBMjfW2cA` | `youtube_micro` | `^PREMARKET LIVE` clips excluded, `max_videos: 10` |
| `bravos` | Bravos Research `UCOHxDwCcOzBaLkeTazanwcw` | `youtube_macro` | macro-thesis channel (`horizon: multi_week`); about 2 long-form uploads a week (15 from 2026-08-09 to 10-03), 10-22 min, published about 15:00-18:00 ET, so the 24 h lookback finds a video on about 2 of 5 mornings (the rest: no info). Paid-package pitch stripped by `sponsor_patterns` |

**Per channel (deterministic, in code):**
1. Flat-list the newest `max_videos` uploads. A listing failure (yt-dlp missing,
   timeout, non-zero exit) is an **error**: the summary shows `✗` and a notice posts.
2. Drop live/upcoming streams, Shorts (≤ 60 s or `/shorts/`) and `title_exclude`
   matches; take the newest remaining video published in the last `lookback` (24 h).
3. None → **no brief today** (`– (no video 24h)`): no info, not a neutral vote, and
   yesterday's brief does not carry over.
4. Transcript: captions first, audio fallback; one caption breaker and one audio
   budget (`yt_max_audio_per_slot`, default 4) for the whole run, so a 429 on one
   channel sends the rest to audio instead of starting four cooldowns.
5. The transcript is stored as a `raw_docs` row (`source_key youtube.<slug>`) and
   closed `scalp_status='brief_only'`: **the 30-min Sweep never reads video**.
6. The channel profile (`arc/ingest/channels/<slug>/profile.yaml` + `GUIDELINES.md`)
   extracts a `ChannelBrief`; every item needs a verbatim quote, and sponsor/promo
   reads are stripped first. The brief expires 24 h after the run and supersedes the
   channel's previous `channel_brief` entry.

**What the Director sees.** A code-built block per YouTube category: `YouTube macro
briefs: 1/2 channels (missing: Bravos)` and `YouTube micro briefs: 3/3 channels`,
each followed by agreement per (ticker, stance) counted over distinct channels
*inside that category* (the category's channel count is the denominator), then
each brief. Each YouTube category is one equal voice among the six; its channels
split it (`trust_weight: 0.5` each); free text never feeds the gate.
A new brief changes the loop's input digest, so the next Director slot runs in full.

**Adding a channel.** One `channels:` entry (slug, `UC…` id or a channel URL,
label, `category: youtube_macro | youtube_micro`, optional `title_exclude` /
`max_videos` / `max_audio_minutes` / `skip_shorts`) plus
`arc/ingest/channels/<slug>/` with `profile.yaml`, `GUIDELINES.md` and fixtures.
No code change; `tests/test_youtube_briefs.py` checks that every configured slug
has a profile.

**Run it by hand** (scratch DB, never `data/arc.db`):

```
.venv/bin/arc routines run youtube.briefs --db ~/.hermes/cache/scratch/yt.db \
  --no-slack --lock-dir ~/.hermes/cache/scratch/yt-lock
sqlite3 ~/.hermes/cache/scratch/yt.db \
  "select channel_slug, video_id, status, expires_at from channel_briefs"
```

The run's manifest metrics carry one record per channel (`outcome`, excluded
videos with reasons, video id/title/published, transcript source, items kept and
dropped by reason, model, tokens, wall time, error).

### 5.23 Finnhub per-ticker context (E4.8, D46)

**What runs.** Four background-lane jobs on the free key `ARC_FINNHUB_API_KEY`
(`~/.hermes/.env`). Each writes one typed context kind per ticker. They are never raw docs, so
they never use the Sweep's D30 budget, and the gate never reads them (import-linter contract):

| Job | Endpoint | Kind (TTL) | When (ET) |
|---|---|---|---|
| `finnhub.insider` | `/stock/insider-transactions` | `insider_activity` (2d) | 06:30 trading days |
| `finnhub.recs` | `/stock/recommendation` | `analyst_recs` (8d) | Mon 06:40 |
| `finnhub.fundamentals` | `/stock/metric?metric=all` (trimmed) | `fundamentals` (8d) | Mon 06:50 |
| `finnhub.earnings_history` | `/stock/earnings` | `earnings_history` (8d) | 07:00 trading days |

`finnhub.earnings_history` fetches the whole scope on Mondays, and on any run once the
last full run is 7 or more days old (`full_every_days`). On other days it fetches only
tickers whose earnings date in the calendar docs was 1–3 days ago.

**One budget for the key.** Every Finnhub caller shares `finnhub_calls_per_minute`
(55 of the key's 60/min), the earnings calendar included. A sliding window kept in
`routine_state[finnhub:calls]` enforces it across processes. A job that has to wait
logs `finnhub.rate_wait wait_s=…`.

**Scope (D51, E12.4).** Open-structure underlyings → live `candidate` subjects → core
→ momentum → trending (today's active list by tier), with ETFs skipped: the names
being traded get context first. It is capped at `finnhub_max_tickers` (50) in that
order, and the cap logs `finnhub.scope_capped dropped=…`. 50 tickers × 3 jobs fit the
shared 55/min budget (the jobs run 10 min apart). The job option `tickers` replaces
the tier part of the scope and `max_tickers` the cap, per job in YAML.

**Outcomes** follow the E4.1d earnings rules:

- no key → `skipped no_api_key`, with one notice per day
- HTTP 403 → `failed forbidden`, meaning a free endpoint went paid, so it fails loudly
- HTTP 429 after one retry → `failed rate_limited`
- more than half the tickers failed → `failed`
- fewer than half failed → `ok`, with `metrics.failed_tickers`

The paid endpoints are never called: economic calendar, dividends, chains, candles,
price target, up/downgrades and social sentiment.

**Run one by hand** (scratch DB):

```
.venv/bin/arc routines run finnhub.insider --db ~/.hermes/cache/scratch/fh.db \
  --no-slack --lock-dir ~/.hermes/cache/scratch/fh-lock
sqlite3 ~/.hermes/cache/scratch/fh.db \
  "select kind, subject, payload from context_entries where status='active'"
```

### 5.24 Finnhub facts in the persona prompts (E4.8a, D46, D44)

The four kinds above reach the Sweep and the Director only when the switch is on:

    personas.finnhub_context: "off"     # config/routines.yaml; off | on

Off (the shipped default), both prompts are byte-identical to the pre-E4.8a prompts
(golden hashes in `tests/test_finnhub_persona_context.py`) and the D31 no-change
digest is unchanged. It is strategy lane: the default flips only on an XP-2 `win`
verdict (`config/experiments/live/xp2_finnhub_context.yaml`, a draft A/B whose
treatment overlay is just this switch; `arc experiment create/register` it after the
A/A). `!arc set personas.finnhub_context on` turns it on for paper without a PR (a
riskier change, so it asks for a confirm).

On, the prompt gets a `Ticker facts (Finnhub, code-built)` block: one line per ticker,
at most `finnhub_context.max_chars_per_ticker` (300) characters, whole parts dropped
in order to fit:

    AAPL: EPS surprise -0.9/+1.1/+4.2/+4.5% (3 beat/1 miss) [1d] | insider 90d net +$2.4M, cluster buy [1d] | analysts net -4 m/m, 64% bullish of 53 [1d] | beta 1.21, 5% off 52w high, …

- **Which tickers:** the Sweep gets the tickers its story digests in each batch name
  (first `sweep_max_tickers`, 8); the Director gets its candidates, highest confidence
  first (first `director_max_tickers`, 10).
- **Missing or stale parts are omitted**, never zero-filled: an expired context entry
  is not in the snapshot, and a part whose fetch date (`as_of`) is older than
  `finnhub_context.max_age_days.<kind>` is dropped. `[Nd]` is the data's age.
- 52-week distances need the ticker's `regime` entry (`last_close`); without one they
  are left out. The market-cap bucket uses `finnhub_context.cap_buckets_musd`.
- **D31 digest:** with the switch on, the loop digest adds `<kind>:<ticker>@<as_of>`
  for the Director's tickers, so a re-fetch of the same day's data is no change and
  a weekly refresh is one.
- The gate never sees any of it, and `Candidate` gains no field.

### 5.25 Director diversification: strict | relaxed (E12.5, D51, D44)

    personas.director_diversification: strict   # config/routines.yaml; strict | relaxed

`strict` (the shipped default) is the E5.9 behaviour: the prompt and the Director
rules are byte-identical to the pre-E12.5 ones (golden hashes in
`tests/test_director_diversification.py`), and the prompt input `diversification` is
not recorded, so `arc journal replay` of older calls is unchanged. `relaxed`:

- **Prompt:** a held name's correlation or a shared industry is not, by itself, a
  reason to exclude; two same-industry names may both rank when each has its own
  catalyst, and the thesis says how the second differs. `adds_concentration` is for
  an add that would push a sector past its flagged level. `portfolio_fit` stays.
- **Deterministic drop** (`_portfolio_filter`): an `adds_concentration` pick drops
  only when its sector is flagged **and** the book (plus picks already kept this
  run) holds `director_diversification.max_names_per_industry` (2) names in its
  industry (`config/sectors.yaml` `industries:`; an unmapped name counts by sector).
  Stance skew alone no longer drops. A pick on an already-held ticker still drops.
- **Flag thresholds:** `director_diversification.relaxed` sector 0.55 / stance 0.85 /
  expiry 0.70 (strict `portfolio.*_max_pct` 0.40 / 0.75 / 0.60; relaxed never goes
  below the strict value).

Unchanged in both modes: the gate's per-underlying cap (`max_alloc_pct`), the
8-position max, the Greek caps, the D33 idea dedupe, and Risk's advisory
`concentration_warning`. Two ways to turn it on: `!arc config
personas.director_diversification relaxed` (riskier, asks for a confirm), or run the
draft A/B `config/experiments/live/xp3_relaxed_diversification.yaml` (XP-3, not
registered). Flipping the shipped default needs an XP-3 `win` verdict.

### 5.26 IV history: `iv_daily`, `iv.record`, backfill, Option Strategist (E4.12, D55)

**Spot.** Every spot used for pricing comes from `arc.data.base.market_spot`: the
quote mid when both sides are > 0 and the spread is at most `spot_max_spread_pct`
(5 %), else today's daily close (a wide quote), else the last close (one-sided, e.g.
after hours `ask=0`, which used to halve TSLA to 171 and read 77 % IV). No spot at
all fails closed for entries (`NoSpotError` / `LookupError` / `PortfolioError`);
exits still price at mids without Greeks. `arc chains` prints `spot … (mid|last_close)`.

**Table.** `iv_daily(ticker, day, source)` holds one 30-DTE constant-maturity ATM IV
per row (decimal). Sources:

| source | written by | use |
|---|---|---|
| `alpaca_cm30` | `iv.record` (15:50 ET, trading days), `arc chains --record-iv`, `arc iv import-csv` | our series (preferred per day) |
| `alpaca_backfill` | `arc iv backfill` | our series where no forward row exists |
| `optionstrategist` | `arc iv import-optionstrategist` (by hand) | cross-check + labelled fallback percentile only, never in the series |

Skipped backfill days sit in `iv_skips` with a reason (re-runs skip them; delete the
rows to retry). IV rank/percentile need `iv_min_obs_rank` (120) observations over the
252-day lookback; below that the regime context shows `iv_percentile_ext` with its
`iv_percentile_ext_source` (`optionstrategist@<date>`, at most `iv_ext_max_age_days` 8
old). IV is context only: `arc.gate` may not import `arc.iv` (import-linter).

**Daily record + [Ops] alert.** `iv.record` (background lane, `writes: []`) records
today's active list + open underlyings + candidates + SPY/QQQ and checks SPY, QQQ and
up to `iv_crosscheck_max_names` (5) names against Cboe's `iv30`. A gap over
`iv_crosscheck_max_pts` (3 vol pts) flags the row; the monitor's `iv_crosscheck`
check opens one `[Ops]` alert (degraded, nothing halted) and resolves it on the
next recorded day within the threshold. The run summary lists `ours/Cboe` per name.

**Backfill (owner-approved one-off on the live DB; rehearse on a copy first).**

    sqlite3 data/arc.db ".backup /tmp/arc-iv.db"
    .venv/bin/arc iv backfill --tickers watch --since 2024-03-01 --db /tmp/arc-iv.db
    .venv/bin/arc iv backfill --tickers watch --since 2024-03-01        # live, resumable

`watch` = today's watch list + open underlyings + SPY/QQQ. Per day: the underlying
close, the expiries bracketing 30 DTE (nearest traded within six per side), the
nearest strike to the close whose call and put both traded (within 3 %), BS inversion
of the two closes (`scanner_risk_free_rate`, `iv_dividend_yields` for ETFs),
total-variance interpolation to 30 DTE. Requests share the
`routine_state[alpaca_data:calls]` budget (`alpaca_data_calls_per_minute`, 150).
About 25 s and 110 requests per ticker-year. Bars are last trades, not mids, and
not simultaneous with the stock close: about ±2 vol pts of daily noise.

**Option Strategist (ad hoc, internal use only — D55).** McMillan's free weekly file
(Saturdays). Never scheduled, never redistributed or quoted outside Arc.

    .venv/bin/arc iv import-optionstrategist [--file saved.html] [--db …]
    .venv/bin/arc iv validate [--tickers watch|all|A,B] [--no-hv] [--db …]
    .venv/bin/arc iv status [--db …]

`validate` compares on the latest OS date: our iv30 vs OS `cur_iv`, our 252-obs
percentile vs OS percentile (plus ours over OS's own `Days` window), our HV20 vs OS
`hv20`, with the E4.12 pass bar. OS `cur_iv` is McMillan's composite implied vol,
not a 30-day ATM constant maturity: compare our forward rows with Cboe `iv30`
(same definition) for method checks; OS is the level/percentile sanity check.

## 6. Local Models (E8.4)

Placeholder — populated by card E8.4 when the 128 GB Mac Studio arrives.
See PLAN.md §2.4 for target: Scalp (cheap tier) routed
locally via llama.cpp or omlx server; that is a `tiers.cheap.model` edit in
`config/llm_routing.yaml` once Hermes has a local provider.

---

## 7. Green main (E1.1b)

Main must stay green. On 2026-09-29 it was red from 17:34 to 18:49Z while three PRs
(#59, #60, #61) merged on top of it, because a test stamped rows with the wall clock
and nothing stopped a merge onto a red main.

### 5.27 Quant <-> Risk open path: `personas.quant_risk_loop` (E13.9, D56, D44)

The open chain's steps are `quant.open` -> `risk.open` -> [`quant.revise`] ->
`quant.propose` -> `broker.execute` (was `quant`, `risk`, `propose`; the old names
still resolve as logged aliases in `routines.yaml`, handlers, triggers and
`monitoring.stuck_after_jobs`, and `arc.journal.legacy` maps stored `routine_runs.job`
rows). `personas.research.chain: auto` is resolved at load by `chain_for()`:

    personas.quant_risk_loop: "off"     # config/routines.yaml; off | on

- **Off (shipped default):** today's chain under the new names; the Quant and Risk
  prompts are byte-identical to the pre-E13.9 ones (golden hashes in
  `tests/test_quant_risk_loop.py`). `quant.propose` rows in the journal are now
  `persona='quant'` (were `system`).
- **On:** Risk gives each structure a verdict (`accept` / `revise` with a
  `revise_request` / `reject`). Rejects never reach a proposal (journal
  `risk_reject`). When any verdict is `revise`, one `quant.revise` round re-chooses
  from the same scanner menu or keeps the first structure (`quant_revised` /
  `quant_kept`); its `structures` entry (`revision_of` = the review id) supersedes the
  first. Risk does not run again; the gate and approval do. The Slack Risk card shows
  verdict chips and the Quant card a `[Quant (revised)]` header.
- **Cost guard:** `steps.quant.revise.min_remaining_s: 90`. The dispatcher skips any
  loop step whose `min_remaining_s` exceeds the `loop.max_runtime` budget left
  (`step_skipped_deadline`); later steps still run. `quant.revise` also skips itself
  (chain continues) with nothing to revise, and on a no-change loop slot.
- Strategy lane: flips only on an XP-7 `win` verdict
  (`config/experiments/live/xp7_quant_risk_loop.yaml`, draft; a paired arm forks at
  `risk.open`). `!arc set personas.quant_risk_loop on` turns it on for paper without a
  PR (asks for a confirm).

### 5.28 Cboe options data: `options_daily` + `vix_futures` (E13.5, D56)

Two `options_slow` sources (`feed: scout`) write typed context, subject `market`,
never raw docs and never gate inputs. Code: `arc/ingest/cboe_daily.py`; recorded
fixtures: `arc/ingest/fixtures/cboe/` (session 2026-10-05).

| Job | Endpoint (free, no key, D3) | Kind |
|---|---|---|
| `options_daily` | `https://cdn.cboe.com/data/us/options/market_statistics/daily/<YYYY-MM-DD>_daily_options` (JSON) | `options_daily`: P/C ratio x6 (total, index, ETP, equity, VIX, SPX+SPXW) with call/put volume, plus call/put/total OI and volume per product |
| `vix_futures` | `https://www.cboe.com/us/futures/market_statistics/settlement/csv?dt=<YYYY-MM-DD>` (CSV) | `vx_curve`: VX monthlies then weeklies (`weekly: true`), front / second / back monthly settle, `slope_1_2_pct`, `shape` |

- **Session read:** a slot at/after 16:30 ET on a session reads that session; earlier
  slots read the previous session (`arc.utils.calendar.completed_session`).
- **Schedule:** `["18:30", "08:15"]` trading days, `catch_up: {until_written:
  "<kind>:{day}"}`. Any slot whose session already has an entry (payload `as_of`)
  is planned `skip-written` (`already written: options_daily for 2026-10-05`), so
  08:15 runs only when the evening slot did not write. Not published (CDN 403/404,
  empty body, or a header-only CSV) = `skipped` at 18:30, `failed` at 08:15.
- **Shape:** `flat` when `|slope_1_2_pct| < options_slow.vx_flat_band` (0.5, tunable
  0-10, Risk none); else `contango` / `backwardation`. Weeklies never set front /
  second / back.
- **Measured publish time** (CDN `Last-Modified` of `_daily_options`, 2026-10-06):
  21:08-22:50 ET over 8 sessions (09-24 21:34, 09-25 21:34, 09-28 21:43, 09-29 21:47,
  09-30 21:39, 10-01 21:14, 10-02 22:50, 10-05 21:08). Today's file is a 403 until
  then and the VX CSV is header-only. So the 18:30 slot normally skips and the 08:15
  catch-up writes the previous session; that is the expected steady state until the
  evening slot moves after ~23:00. To re-measure, set
  `options_slow.publish_probe_minutes` (0-60, measurement only, not runtime-tunable):
  an evening run then re-probes once a minute and records `probe_wait_s`.
- **Terms:** Cboe market statistics are published for personal, non-commercial use.
  Arc stores them for its own decisions and shows them only on internal surfaces
  (Slack workspace, Tailscale-only Tower); no redistribution or republication.
- `put_call` (E4.5) still writes the `put_call` kind via a thin wrapper over the new
  parser for d51 readers; E13.15 removes it. `vol_term` (VIX index closes) stays as
  `market_guard`'s VIX source.
- Check: `arc routines run options_daily --db <scratch> --now <date>T18:30-04:00
  --no-slack`, then `arc context show --db <scratch> --kind options_daily --latest`.

### 7.1 Required status check: `check`

`.github/workflows/ci.yml` job `check` (job id and `name:` both `check`) runs
`uv sync --locked` + `make check` (lock-check, lint, format, pip-audit, full suite,
gate at 100% branch coverage). It is **the** required status check on `main`. Keep the
name stable: branch protection matches checks by name, so renaming the job quietly
removes the requirement.

Merge rules:

- Never merge a PR whose head `check` job is not green.
- Never merge onto a red main. If main is red, the next PR to merge is the fix.
- A worker never merges its own PR (AGENTS.md).

**Owner-only step (one time).** Worker tokens cannot set branch protection
(`gh api repos/mohitgulla/Project-Arc/branches/main/protection` returns 403). In
GitHub: Settings → Rules → Rulesets → New branch ruleset → target `main` →
enable "Require status checks to pass" → add `check` (and "Require branches to be up
to date before merging") → Save. Check it with
`gh api repos/mohitgulla/Project-Arc/rules/branches/main`, which should list a
`required_status_checks` rule containing `check`.

### 7.2 Tests never read the wall clock

Tests inject a fixed `now` (a module-level `NOW`, a `now=` argument, or
`monkeypatch.setattr("<module>.now_et", lambda: NOW)` for code that reads the clock
itself). `tests/test_no_wall_clock_in_tests.py::test_no_wall_clock_reads_in_tests`
parses every file under `tests/` and fails on any `now_et()`, `datetime.now()`,
`datetime.utcnow()`, `datetime.today()` or `date.today()` call. It runs in `make test`,
so in `make check` and CI.

A live integration test that genuinely needs real time (live quotes, RTH checks, a
hook subprocess verifying real token expiry) marks the call with
`# wall-clock: <reason>` on the same line or in the comment block directly above it.
Everything else pins the clock.
