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
| stuck_runs | A `routine_runs` row is still `running` after `stuck_after` (70m), or after its job's `stuck_after_jobs` override (`monitor: 10m`, E5.3a). | `stuck:<run_id>` |
| gateway | `hermes gateway status` or `hermes cron status` shows a `✗`, exits non-zero, or times out. `⚠` warnings count as degraded: they are recorded but not alerted unless `gateway.alert_on_degraded: true`. | `gateway` |
| remote_access (E8.6, off until enabled) | `GET <ts-ip>:1994/api/status` doesn't answer, or answers without `auth_required: true` and `basic` in `auth_providers`; `GET <ts-ip>:4174/_stcore/health` isn't 200; or either port accepts a connection on a LAN address. See §5.7. | `remote_hermes`, `remote_tower`, `remote_exposed` |

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
Tailscale address, port 4174. Code: `arc/tower/`.

```
arc tower serve                   # http://<tailscale-ip>:4174, re-reads every 30s
arc tower serve --local           # 127.0.0.1:4174, this machine only
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

The intraday `monitor` routine runs every 5 minutes in session (D35; `personas.monitor`
in `config/routines.yaml`) and writes one `heartbeats` row per run (`component = monitor`):
Greeks, `equity`, `last_equity`, `cash`, `buying_power`, `options_buying_power`,
`non_marginable_bp`, `broker_requests` (the run's request estimate), and the broker legs
with `current_price`, `lastday_price` and `change_today` when Alpaca reports them. The
tower reads only this row, so marks are at most ~6 minutes old in session.

**Stale rule (E5.3a).** Greeks are flagged stale once the latest monitor row is older than
3 × the monitor's `every` (15 minutes at 5m). The cadence is read from the effective routines
config (`routines.yaml` plus any `!arc set routines.monitor.cadence` override), so changing the
cadence moves the threshold with no code change. The snapshot exposes it as
`greeks.stale_after_s` (`arc tower snapshot --json`), which the Streamlit page and the E8.7 API
share. Outside the session the page shows the last in-session run.

Broker load: each run makes 2 + 4 × (open underlyings) Alpaca requests (account, positions,
then quote, chain snapshot, contracts page and volume snapshot per underlying): 34 at the
default 8 open positions, one run per 5 minutes, far below Alpaca Basic's 200 requests/min.

Quick check of the fields on a DB:

```
sqlite3 data/arc.db "select at, json_extract(detail,'$.equity'), json_extract(detail,'$.buying_power') from heartbeats where component='monitor' order by at desc limit 3"
```

#### v2 (preview, E8.7 / D35)

A FastAPI JSON API plus a React SPA (design spec: `docs/TOWER_DESIGN.md`). It runs
alongside Streamlit until the E8.7e cutover: `arc tower serve` is still Streamlit and
`--v2` starts the new tower. Code: `arc/tower/api.py`, `arc/tower/routes/`, `web/`.

```
make web                                    # build the SPA into arc/tower/static/ (needs Node >= 22.12)
arc tower serve --v2                        # http://<tailscale-ip>:4174, uvicorn
arc tower serve --v2 --local --port 4180    # 127.0.0.1 only (Streamlit can keep :4174)
arc tower serve --v2 --refresh 30           # client poll interval: 30 | 60 (default) | 120 s
arc tower serve --v2 --print-command        # show the uvicorn argv, don't start
curl http://<addr>:<port>/api/health        # {status, db, as_of}
```

- **Endpoints (GET only):** `/api/health`, `/api/meta` (version, git sha, `ARC_ENV`,
  account profile, `config_version`, `monitor`/`auditor`/`tick` cadences with stale =
  3 × cadence, gate caps), `/api/snapshot` (the `arc tower snapshot --json` payload).
  Every response carries `as_of` (ET); errors are `{error, detail, as_of}` (e.g. 503
  `db_unavailable`). `/api/docs` is the OpenAPI browser. `/kitchen-sink` shows every
  design-system component in both themes.
- **Bind rules** are the Streamlit ones above: Tailscale or loopback only, exit 2
  without a Tailscale address, on `0.0.0.0`/a LAN IP, or when the DB is missing.
- **Read-only.** Each request opens the DB `mode=ro` + `query_only`; the app has no
  POST/PUT/DELETE route (a test checks the OpenAPI spec), and the import-linter
  contract `arc.tower is read-only` forbids the broker, market data, personas, LLM
  routing, Alpaca and Slack. D26 config overrides are read from the store on every
  request, so a Slack `!arc config` change shows without a restart.
- **No auth, no CORS.** Same as Streamlit: the tailnet-only bind is the boundary.
  Don't bind it anywhere else.
- **Offline.** Inter is bundled (`@fontsource/inter`); the page fetches nothing
  outside the tower.
- **Dev:** `make web-check` (eslint, tsc, vitest for `web/src/lib/format.ts`),
  `make web-api` after changing a route or response model (regenerates
  `web/openapi.json` and the typed client; a pytest fails if they drift),
  `make web-e2e` (Playwright screenshots at 390×844, 768×1024 and 1440×900 in both
  themes, against a scratch DB; set `PLAYWRIGHT_BROWSERS_PATH` to a dir with
  Chromium, e.g. `~/.hermes/tools`).

### 5.7 Remote access over Tailscale (E8.6, D29)

Use Hermes (chat, sessions, cron, kanban, config) and the control tower from a
laptop or phone on the **same Tailscale tailnet** as the Mac Mini. Nothing listens
on the LAN or the internet.

| Service | URL | LaunchAgent | Runs |
|---|---|---|---|
| Control tower (read-only, §5.6) | `http://<ts-ip>:4174` | `com.projectarc.tower` | `.venv/bin/arc tower serve` |
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
- *Host header: use the IP.* The dashboard only accepts requests whose `Host` is the address
  it bound to. With a `100.x` bind, `http://<ts-ip>:1994` works; `http://mac-mini:1994` and
  `http://mac-mini.<tailnet>.ts.net:1994` return **400 "Invalid Host header"**. To use the
  MagicDNS name instead, set `dashboard.public_url: http://<name>.<tailnet>.ts.net:1994` in
  `~/.hermes/config.yaml` (exactly one hostname is trusted). Not needed for the runbook.
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
5. **Start both services:** `~/GitHub/Project-Arc/hermes/remote/install.sh`
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
running `ARC_ENV`, the chain step `execute` (right after `propose`, and after
`risk.reallocate` in the position-manager chain) publishes that chain's proposal cards,
approves them as `arc:auto-approve` (card marked `Auto-approved (paper|LIVE)`, no buttons),
and hands each one to an Investor subprocess (`arc routines run investor --event <id>`,
its own per-event lock, never the LLM lock), so the ladder starts in the same tick. When it
is off, `execute` reports `awaiting approval (N cards)` and the click flow is unchanged.

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

### 5.11 Open universe (E5.7, D9/D28)

`settings.universe` is a seed list (`ARC_UNIVERSE_MODE=seed`, the default). Other
names reach the Scout only if they are in the symbol master and pass the liquidity
screen in `config/universe.yaml`. `ARC_UNIVERSE_MODE=strict` restores the allow-list.

1. First install (once, before the first seed-mode Scout run; needs the paper keys):
   `set -a; source ~/.hermes/.env; set +a; arc universe refresh`
   (about 10k symbols; writes `data/symbol_master.json`). After that the weekly
   `symbols` routine (Mon 05:30 ET) keeps it fresh.
2. Check it: `arc universe status` (exit 1 = missing cache; ingest then uses the seed
   list only and the Scout rejects non-seed names as `unknown_symbol`).
3. Would a name be admitted? `arc universe check PLTR HOOD` (read-only market data;
   `--fixture` = offline). The same checks run in the Scout; rejects are journalled
   as `universe:<reason>` and shown on the Scout card.
4. Knobs: `scout_max_new_tickers` (Slack-tunable, ceiling 25), `universe_mode`,
   `pipeline_max_shortlist` (the Quant/Risk budget, not a Director cap).

### 5.11 Source fairness + options data (E4.5, D30)

Every Scout source is a named entry in `config/routines.yaml`: each RSS feed under
`sources.rss.feeds` (`name`, `url`, optional `label`, `category`, `weight`,
`max_docs_per_run`, `hosts`), and `edgar` / `earnings` / `youtube.*` with a
`category` and `label`. Adding or re-weighting a source is a YAML edit only.

- **Budget.** Each Scout run reads `scout_doc_budget` docs (default 120, Slack-tunable
  20-400), shared by weighted round-robin (equal weights by default; per-category
  split via `personas.scout.category_weights`). Newest first within a source. Docs
  over budget wait for the next run; once older than the `raw_doc_ref` TTL (5d)
  they are closed `raw_docs.scout_status='skipped_budget'` with the run id. Never
  deleted. The Scout card's *Source mix* fact shows `read (N over budget)` per source.
- **Stories.** Near-duplicate headlines (Jaccard ≥ `scout_story_threshold` within
  `scout_story_window_hours`) are one story; EDGAR filings group by filer + form.
  Corroboration on a candidate = distinct *sources* behind its URLs, computed in code
  (`candidates.corroboration`); the LLM cannot raise it.
- **Two stages.** Stage 1 digests stories in batches of `scout_batch_size` on the
  Scout's (cheap) model; evidence quotes must appear verbatim in a doc, else dropped;
  a failed batch falls back to an extractive digest (headline + lead). Stage 2 reads
  the digests, `scout_story_batch_size` (40) per Scout call. Both stages'
  tokens/cost land in `scout_batches`
  (`stage` = `digest` | `scout`) and the run manifest.
- **Options data** (free, no key; typed context kinds, read by the Director and Risk):

  | Job | Source | Kind | When (ET) |
  |---|---|---|---|
  | `vol_term` | Cboe VIX9D/VIX/VIX3M/VVIX daily history | `vol_term` (contango/backwardation) | 09:00, 16:45 |
  | `put_call` | Cboe daily market statistics | `put_call` | 09:00 |
  | `macro_calendar` | federalreserve.gov FOMC page + BLS release ICS | `macro_calendar` | 05:45 |
  | `unusual_options` | Alpaca chain snapshots (self-computed) | `unusual_options` per ticker | 12:30, 15:45 |
  | `ex_dividend` | Alpaca corporate actions | `ex_dividend` per ticker | 06:15 |

  BLS rejects a User-Agent without a contact email (403): it is sent
  `ARC_EDGAR_USER_AGENT`, same as EDGAR. UOA flags a ticker when its option volume is
  ≥ `uoa_volume_spike_ratio` × its 20-session average (needs 5+ sessions of
  `options_volume_daily` history, which the job builds itself), or when lines with
  vol/OI ≥ `uoa_vol_oi_ratio` (≥ `uoa_min_dte` DTE, OI ≥ `uoa_min_open_interest`)
  carry ≥ `uoa_min_hot_share` of its volume.

## 6. Local Models (E8.4)

Placeholder — populated by card E8.4 when the 128 GB Mac Studio arrives.
See PLAN.md §2.4 for target: Scout/Investor/Auditor (cheap tier) routed
locally via llama.cpp or omlx server; that is a `tiers.cheap.model` edit in
`config/llm_routing.yaml` once Hermes has a local provider.
