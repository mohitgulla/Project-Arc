# Project Arc — Implementation Plan (Plan of Record)

**Status:** v0.1 · 2026-09-27 · owner: Mohit Gulla · executed by Hermes Agent
**Tracking:** Hermes Kanban board `project-arc` (bound to this repo) · dev comms `#project-arc` (one thread per card) · trading comms `#arc-investor`

> *Reasoning models propose; only deterministic code and an explicit human click can authorize capital at risk.* — Project Arc brief, §7

---

## 0. Decisions log

Decisions confirmed with the owner on 2026-09-27. Anything not listed here is a default and can be revisited; anything listed here changes only by an explicit decision recorded in this section.

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| D1 | Phase-1 broker | **Alpaca paper account** (official options API, `mleg` multi-leg orders, official MCP server v2, free indicative options feed) | Brief named Robinhood, but Robinhood's official Agentic MCP (`agent.robinhood.com/mcp/trading`, May 2026) has **no paper mode, single-leg only, localhost-only OAuth**; `robin_stocks` is ToS-risk. Broker is an adapter interface so venues can be added later. Alpaca has no index options (SPX) — Section 1256 treatment is deferred. |
| D2 | Persona presentation in Slack | **Single Hermes bot** (`hermes` app, user `U0C4UH9TT5X`) posting persona-labelled messages in threads | One credential; personas are internal roles (skills + JSON schemas). Can split into per-persona profiles later without changing the pipeline. |
| D3 | Compute | **Cloud-only now**: every card implements against the cloud frontier model paths (Anthropic subscription, tiers per D8 / `config/llm_routing.yaml`); no local-model code, servers or benchmarks until then. Local models (E8.4) stay **blocked until the 128 GB Mac Studio arrives** and are picked up then. Fallback provider: none for now (D8). | Owner reconfirmed 2026-09-27: park E8.4, no hardware-independent prep card. | Current Mac is M6 / 16 GB. Qwen3.8-Flash-Next is real (2026-08-26) but needs ~99 GB. |
| D4 | Phase-1 strategy scope | **Defined-risk only**: vertical spreads, iron condors, plus single-leg long calls/puts (debit). Liquid ETFs + ~20 large caps. 30–45 DTE entries, 16–30Δ short strikes. Brief's limits as defaults. | Evidence: management rules don't beat hold-to-expiry statistically; 21-DTE exits only matter for undefined-risk; defined-risk caps tails structurally. |
| D5 | Kanban execution | **Auto-dispatch**: worker profile claims cards, implements in git worktrees, PR completion contract, review gate before merge. `max_in_progress: 1`. | Owner choice. Board is released by completing the P0 gate card. |
| D6 | Python | **3.12 via uv** (`~/.hermes/tools/uv-0.12.3`) | All required wheels resolve on 3.12 and 3.13; OpenBB (optional) pins ≤3.12. |
| D7 | Market data | **Alpaca Basic (free, indicative feed)** for Phase 1; ThetaData free EOD tier for backtest history; upgrade path: Alpaca Algo Trader Plus ($99/mo OPRA) or ThetaData Value ($40/mo) | Cheapest path that still gives chains + Greeks. Decide on paid tier only after the backtester exists (E7). |
| D8 | LLM provider | **Anthropic Subscription (Claude Opus 5.5)** for all implementation/execution tasks. No fallback provider for now and none required: E8.1 only leaves a TODO note in docs/OPS.md to wire one in later. Persona model tiers (§2.4), both on the subscription: **frontier = `anthropic/claude-opus-5.5`** (Director, Quant, Risk); **cheap = `anthropic/claude-opus-5`** (Scout, Investor, Auditor). | Single provider simplifies Phase 1; subscription already in place. Tiers refined by owner 2026-09-27 (E8.1). |
| D9 | Universe | **Revised 2026-09-28 (D28): seed list, not an allow-list.** Seed: SPY QQQ IWM DIA XLF XLE XLK AAPL MSFT NVDA AMZN GOOGL META TSLA AMD JPM BAC XOM UNH HD (always scanned). Any other US optionable ticker the sources surface may become a candidate if it is in the SEC symbol master and passes the deterministic liquidity screen (`config/universe.yaml`). `ARC_UNIVERSE_MODE=strict` restores the allow-list. | Starting set covers liquid ETFs + large caps; owner wants discovery beyond a static list. |
| D10 | Approval policy | **Owner-only** (`U0C5KUMH28G`). Add a **self-approval / auto-approve mode** for paper account (configurable flag `ARC_AUTO_APPROVE=true`, paper-only, enforced by gate). | Lets paper pipeline run fully autonomous for evaluation. |
| D11 | Push policy | **Auto-push**: workers push branches and open PRs automatically (completion contract). | Owner preference; review gate still required before merge. |
| D12 | Persona names | **Scout, Director, Quant, Risk, Investor, Auditor** (Execution/Exec renamed to Investor). Slack labels: `[Scout] [Director] [Quant] [Risk] [Investor] [Auditor]`. | Owner choice 2026-09-27. |
| D13 | Initial YouTube source | **StockedUp** (channel `UC-m6zNItyoDk5lSykDlhE4Q`), default in `ARC_INGEST_YOUTUBE_CHANNELS`. Posts a next-session outlook almost every trading day. | Owner choice 2026-09-27. |
| D14 | Channel processors | Each YouTube channel gets a **profile + extraction guidelines** (`arc/ingest/channels/<slug>/`). Newest video → validated `ChannelBrief` (schema §E4.4). TTL is **per source** (`ttl_sessions` in the profile). **StockedUp = 1 session**, and its next video supersedes it (`supersede: latest`). Macro-guidance sources can set longer TTLs (e.g. 5–20 sessions, `supersede: accumulate`). Expired briefs never inform trading. Every extracted item carries a verbatim transcript quote, checked deterministically; items that fail are dropped. | Owner: newest video informs next-day trading until a new video replaces it; recent info for daily sources, longer horizon for macro. |
| D15 | Scout cadence + transcription | Scout runs **twice daily at 22:00 ET** (after-close sources, e.g. StockedUp) and **12:00 ET** (midday news), processing all new sources (YouTube, RSS, EDGAR, earnings). Videos without captions fall back to **local audio transcription** (mlx-whisper, no paid STT). **Caption rate limits (E4.1c):** YouTube's timedtext endpoint 429s this IP after a few requests (no `Retry-After`, Google "Sorry" page). Caption requests are paced (`ARC_YT_CAPTION_SLEEP_SECONDS`, default 5 s). The first 429 in a run trips a per-run breaker (no more caption requests that run; remaining videos go to audio under the usual grace/cap rules) and sets a DB-persisted cooldown (`ingest_cursors` row `youtube:captions_backoff`) of `min(base·2^(n-1), max)` ±10% jitter (defaults 30 → 360 min; `Retry-After` wins if larger). Runs inside the cooldown skip captions. The first successful caption download resets the streak. An empty 200 (PO-token gating) is logged `captions_empty` and never starts a cooldown. No cookies/PO-token providers without owner approval (D3). | Owner choice 2026-09-27; backoff policy E4.1c. |
| D16 | Orchestration | **Config-driven routines** (`config/routines.yaml`). Every source and persona has its own cadence (`schedule` / `every`+`window` / event `trigger`), and personas can be **chained** (e.g. Director→Quant→Risk→propose). A single Hermes cron runs `arc routines tick` every 5 min. All agent outputs go to an append-only **context store** (`context_entries`, TTL + supersede). Downstream agents read a `ContextSnapshot`, and its id is recorded on every run for audit/replay. | Owner: flexible per-source frequency, per-persona cadence, chained executions, shared DB context. |
| D17 | Build order | **New work starts from latest `main`, after the open PRs it depends on have merged.** A card whose parents (or the PRs they depend on) are unmerged waits in `triage` with an `[awaiting-merge]` gate. The arc-board plugin promotes it once every parent PR is merged, and the worker rebases on `origin/main` first. Workers never stack on unmerged branches. | Owner choice 2026-09-27: avoids conflict-resolution by workers and stale bases. |
| D20 | Card thread lifecycle | Every card gets its own #project-arc thread when it is created (gatekeeper cron, 1 min). When the card is done **and** its PR has merged, the thread's parent message is edited to :white_check_mark: with the PR link. Any issue found after merge is discussed **in the original card's thread** until a follow-up fix card is agreed. The fix card then gets its own thread, and the original thread gets a pointer to it. | Owner choice 2026-09-27. |
| D18 | Paper sizing | **Not fixed at 1 contract.** `contracts = min(Risk.sizing_suggestion, floor(5% equity / max_loss_per_contract))`, minimum 1 when a single contract fits under the 5% cap; otherwise no trade. The gate still enforces every portfolio cap. | Owner choice 2026-09-27 (answers E5.2 Q2). |
| D19 | Exit management + reallocation | Open positions are re-evaluated on the intraday routine. **Investor** proposes **early profit-taking closes** (per-structure profit target, e.g. 50% of max gain for credit structures, plus a time-decay-adjusted target, with no waiting to expiry). **Risk** can propose **close-to-reallocate**: close a position to free buying power when a new candidate's expected risk/reward, net of costs and slippage, beats the open position's remaining EV by a configured margin. Every close is a Proposal that goes through gate + approval (`ARC_AUTO_APPROVE` applies in paper). | Owner choice 2026-09-27. Note: D4's evidence says management rules don't beat hold-to-expiry *statistically* for defined-risk. E7.2 backtests D19's rules against hold-to-expiry, and the paper scorecard reports both. |
| D22 | Review-friendly posts + decision journal | Every persona post in #arc-investor uses one Block Kit layout (`arc/slack/blocks.py`): title `[Persona] <What>: <subject> • <fact> • <fact>` (proposals: `[Quant] Proposal: SPY • Oct 30 (35 DTE) • Iron Condor`), a summary line, a fact grid, persona-attributed reasoning, and audit ids in the footer. The E6.1 card also shows each persona's reasoning for the trade (Director rank/confidence/thesis, Quant rationale, Risk rating and suggested vs sized contracts, regime/vol). Every decision (selected, rejected, no-trade, approved, expired) is written to an append-only **decision journal** with a stable `reason_code`, its inputs snapshot and persona call. Outcomes are attributed back to decisions (P&L vs EV, PoP calibration, slippage vs cost, D19 shadow hold). Reviews label decision quality separately from outcome, plus a root-cause category. A Reject click opens an **optional reason** modal; the decision counts on the click and the reason is journaled as a follow-up. Other personas' cards are E5.5; the journal is E7.4. | Owner 2026-09-27: presentable, detailed review of every proposal; find suboptimal gaps and the root cause of good/bad decisions. |
| D23 | Exit policy + managed EV | One `ExitPolicy` per structure kind in `config/exits.yaml` (take profit % of max gain / % of debit, stop, close-at-DTE, time-adjusted targets). A deterministic Monte Carlo model (GBM at ATM IV, daily repricing, `CostModel` costs) gives **managed** PoP / Net EV next to the static hold-to-expiry numbers. The same policy drives the proposal card, the Investor's live exits (E6.2/E6.4, via `evaluate_position`) and the backtester's `exit_policy` (E7.2). **Net EV** = expected value after spread, slippage, commission and regulatory fees, from one cost config shared with the backtester. **Stop defaults are relaxed** so a trade can play out: credit 75% of max loss, debit 75% of debit, evaluated on end-of-day marks only. Take profit is 50% of max gain (credit) or 100% of debit (debit); close at 7 DTE. The proposal card shows net EV, a cost & liquidity breakdown (top of book only on Alpaca), spot and moneyness, vol stats (IV, IVR, IVP, HV20/60) and the exit plan (E6.1a). | Owner 2026-09-27: static expiry math misstates realised EV/PoP under active management. |
| D24 | Order price improvement | **Price-band GateToken (`arc2`).** The gate evaluates the whole band `[mid .. worst limit after N steps]` and mints one token bound to (hash, legs, qty, band, max_steps). One approval authorises the band, whose worst price is shown on the card. `submit()` checks each step is inside the band; `client_order_id = token.s<k>`. Defaults: N=3 steps, each 1/3 of the way to the far touch, 60 s per step, then cancel. | Owner 2026-09-27 (answers E6.2's question, option A). |
| D25 | Account profiles + ranking by evidence | **Account profile** (`config/account_profiles.yaml`, `ARC_ACCOUNT_PROFILE`): `margin` (D4 set), `cash_debit` (long calls/puts + debit verticals, net debit, short legs covered, fits settled cash), `cash_long_only`. **Paper default = `cash_debit`**, to mimic the expected no-margin real-money account; `margin` is one switch away. The gate enforces the profile (rule `account_profile`); the scanner gains debit strategies; neutral stance = no trade under `cash_debit`. **Ranking:** the default stays `credit_width` until the E7.5 walk-forward backtest compares it with `ev`, `managed_net_ev` and `rorc_day` (managed Net EV ÷ max loss ÷ expected days held) per profile. The switch rule is fixed in advance: beat the incumbent on net P&L **and** max drawdown in ≥2 of 3 sub-periods, with a 90% bootstrap CI excluding 0. | Owner 2026-09-27: real money likely has no margin, so credit spreads are unavailable. |
| D26 | Strategy control panel | Owner-only Slack control panel: `!arc config / set / diff / history / revert / profile`, with a CLI mirror. Every tunable key has a registry entry with bounds and a hard code ceiling. Overrides live in an append-only `config_changes` table and apply at the next tick with no restart; each run records its `config_version`. Riskier-direction changes need a confirm step. `ARC_ENV`, gate code and secrets are never tunable. The gate always reads the effective config. | Owner 2026-09-27. |
| D27 | Persona I/O contracts + run manifest | Every job/step in `config/routines.yaml` declares `reads` **and** `writes` (context kinds). `JobContext.write` rejects undeclared kinds (fail-closed), and a test requires every job to declare. Free-form persona text (thesis, regime view, observation, risk flag, lesson, execution note) is stored as a typed `note` kind (`NotePayload`: persona, topic, horizon, stance, title, body, confidence, tags, evidence, about), not discarded. Notes are context only and are never gate or `Candidate` inputs. Payload JSON Schemas are committed under `schemas/context/<kind>.v<N>.json`, and a model change without a version bump fails CI. Every run (ok/failed/skipped) writes an append-only **run manifest** with all run metadata: identity, trigger, timing, session, outcome, environment flags (never secret values), git sha, hashes of every config file, E8.5 config_version, the effective spec, declared contract and kind schema versions, input snapshot/digest, external market/broker inputs (as_of + digest), outputs by kind, LLM models/tokens/latency/cost, and linked decisions, proposals, gate decisions and Slack posts. Every log line carries run_id/chain_run_id. `arc context trace <chain|run>` shows all of it. | Owner 2026-09-27: consistent, structured input/output for every persona (chained or cron), including thesis/regime/informational text, for reuse, tracking and audit. Undeclared writes fail the run, and all necessary metadata is logged, not only named fields. |
| D28 | Wider funnel, minimal filtering | Scout: open universe (D9 revised), cap `scout_max_new_tickers` (10) non-seed names per scan, liquidity screen deterministic and journaled. Director **ranks every candidate it would trade** with thesis + evidence; exclusions need a stated reason; `pipeline_max_shortlist` (default 10) is a downstream Quant/Risk budget, not a Director cap. Quant: one structure or a stated skip per ranked ticker. Risk: one assessment per structure, one repair re-ask, else `not_assessed` journaled. Risk owns portfolio-aware sizing (D18); Quant stays per-contract. Gate unchanged. Investor: each attempt's cancel is confirmed before the next limit (D24, N=3 steps = 4 attempts). Auditor card: Day / MTD / YTD P&L with % of equity, from E6.3 `pnl_snapshots`. | Owner review of E5.5 cards, 2026-09-28. |
| D21 | CLI verbs | `arc scan` = Scout candidate pipeline (E4.2). The option-chain scanner (E2.3) is `arc chains SPY --dte 30-45 --delta 20`. | Owner choice 2026-09-27; both PRs claimed `arc scan`. |
| D23 | Independent daily audit (Arc Sentinel) | A Hermes cron in its own profile `arc-sentinel` (14:00 PT daily, `claude-fable-5.1`, reasoning `max`) audits latest `main` from a private clone, only when the SHA changed since the last review. Deterministic checks run first: lint, format, import contracts, unit suite + coverage, gate 100%, live paper integration, CLI smokes, lock freshness, pip-audit, plus metric deltas vs the last review. Then an independent review covers trading-safety invariants, regressions, tests, CI/build/release, plan drift, strategy and cited web research. Findings get stable S-ids in a ledger (new/open/resolved/regressed) and are reported in one #project-arc thread, which is routed to the Sentinel profile (`gateway.profile_routes`). Cards are created only on the owner's `create S-<n>`. The auditor is self-contained: its own profile, skill and SOUL, no shared memory, it never reads kanban comments/worker output/PR threads, and it never edits code. | Owner 2026-09-27: self-healing quality loop independent of the implementation agents; midday run; separate profile. |

Open items requiring a decision are listed in §9 — all five original items are now resolved (D8–D11 + keys stored).

---

## 1. Goal and non-goals

**Goal.** A self-hosted, closed-loop, agentic US-equity options trading system: ingest ideas → LLM personas propose structured trades → deterministic pricing/Greeks → deterministic risk gate → human Approve/Reject in Slack → broker order via an adapter → audit + reconcile → feed back. Paper trading first; live only after a written go-live review.

**Non-goals for Phase 1.** Live capital. Undefined-risk structures. 0DTE. Index options. Local LLM inference. Streamlit control tower (Phase 3). Multi-venue routing.

---

## 2. Architecture

### 2.1 DAG (maps the example diagram onto Arc)

```
 Market Data & Math Engine [Deterministic]   Information Retrieval [Scout persona]
        │  chains, IV, history                      │  RSS · EDGAR · earnings cal · YouTube
        ▼                                           ▼
 Greek Analysis [Deterministic]   ──────►  Aggregator [Director persona]
   Δ Γ ν Θ ρ Vanna Volga, payoff              candidate objects + regime features
                                                    │
                        ┌───────────────────────────┴────────────────────────────┐
                        ▼                                                        ▼
        Risk/Reward Analysis [Quant persona]                     Portfolio Alignment [Risk persona]
        structure selection, PoP, EV, cost-aware                 concentration, Greek budget, calendar
                        └───────────────────────────┬────────────────────────────┘
                                                    ▼
                          ┌──────── RISK PROXY GATE [Deterministic Python, non-bypassable] ────────┐
                          │ 5% per-underlying · 3% daily loss halt · spread/tick · wash-sale 30d   │
                          │ portfolio Δ/ν caps · defined-risk whitelist · earnings blackout · halt │
                          └──────────────────────────────┬───────────────────────────────────────┘
                                                         ▼
                              Decision Processor [Human — Slack clarify Approve/Reject]
                                                         ▼
                              Trade Execution [Investor persona → BrokerAdapter.alpaca_paper]
                                                         ▼
                              Auditor persona · SQLite audit · reconciliation ─► back to Aggregator
```

Hard boundaries (enforced in code, not prompts):

1. **Personas never call the broker.** The only code path that can submit an order is `arc.execution.submit()`, which requires a `GateToken` (HMAC over the exact order payload, minted by the gate) **and** an `ApprovalRecord` (Slack approval id) for the same payload hash.
2. **Hermes `pre_tool_call` hook (fail-closed)** blocks any tool whose name matches the broker order tools unless the token check passes — defence in depth for the MCP path.
3. **The gate has no LLM inputs.** It reads the proposal, the audit DB, live account state, and config. Pure functions, 100% branch coverage required.
4. **`ARC_ENV=paper` is the default and `live` requires a separate credential file** that does not exist in Phase 1.

### 2.2 Components and repo layout

```
Project-Arc/
├── AGENTS.md                 # conventions for Hermes workers (loaded from cwd)
├── docs/PLAN.md              # this file
├── docs/ARCHITECTURE.md      # diagrams + data contracts (E1.1)
├── docs/DECISIONS/           # ADRs, one file per decision after D7
├── pyproject.toml            # uv, Python 3.12, ruff, pytest
├── arc/
│   ├── config.py             # pydantic-settings; ARC_ENV; limits; universe
│   ├── calendar.py           # exchange_calendars: sessions, early closes, DTE
│   ├── store/                # SQLite schema + migrations + repositories
│   ├── data/                 # MarketDataProvider protocol; alpaca, thetadata
│   ├── pricing/              # BS + Greeks (py_vollib, QuantLib cross-check)
│   ├── structures/           # legs → payoff, max gain/loss, breakevens, net Greeks
│   ├── scanner/              # chain filters, IVR, delta-targeted strikes
│   ├── ingest/               # rss, edgar, earnings, youtube → Candidate
│   ├── features/             # regime (Markov 3-state), IV/HV, IVR
│   ├── personas/             # JSON schemas + prompt builders (no side effects)
│   ├── gate/                 # rules.py (pure), token.py, halt.py
│   ├── approvals/            # Slack proposal card, TTL, ApprovalRecord
│   ├── execution/            # order state machine, submit(), fills, exits
│   ├── broker/               # BrokerAdapter protocol; alpaca_paper.py
│   ├── reconcile/            # broker vs local, PnL snapshots, alerts
│   ├── backtest/             # cost-aware engine, walk-forward, reports
│   └── cli.py                # `arc scan|chains|propose|gate|approve|execute|reconcile|report`
├── hermes/
│   ├── skills/arc-*/SKILL.md # persona skills (Scout, Director, Quant, Risk, Investor, Auditor)
│   ├── hooks/arc-gate/       # pre_tool_call fail-closed hook
│   └── routines/             # cron job definitions (pre-market, intraday, post-market)
└── tests/                    # unit, property (hypothesis), integration (paper account)
```

### 2.3 Data contracts (pydantic, versioned)

- `Candidate` — ticker, stance, catalyst_type, catalyst_date, confidence, sources[], created_at (from Scout; §5 of brief).
- `Structure` — legs[] (occ_symbol, side, ratio, intent), net_debit_credit, max_gain, max_loss, breakevens[], greeks{Δ Γ ν Θ ρ vanna volga}, dte, liquidity{spread_pct, oi, vol}.
- `Proposal` — candidate_id, structure, thesis (persona text), quant{pop, ev, cost_bps}, risk_narrative, sizing{contracts, notional, pct_equity}, expires_at.
- `GateDecision` — proposal_hash, passed, violations[], token (if passed), account_snapshot.
- `ApprovalRecord` — proposal_hash, slack_user, slack_ts, decision, at.
- `Order` — state machine: `proposed → gated → approved → submitted → partially_filled → filled | cancelled | rejected | expired`; every transition is an event row.

### 2.4 Personas (adapted from AutoHedge, options-specific)

| Persona | Input | Output (JSON schema) | Model tier | Slack label |
|---|---|---|---|---|
| **Scout** (Information Retrieval) | raw RSS/EDGAR/transcripts | `Candidate[]` | cheap | `[Scout]` |
| **Director** (Aggregator) | candidates + regime + portfolio | ranked shortlist + thesis per ticker | frontier | `[Director]` |
| **Quant** (Risk/Reward) | shortlist + chains + Greeks | `Structure[]` with PoP/EV/cost, confidence | frontier | `[Quant]` |
| **Risk** (Portfolio Alignment) | structures + portfolio + calendar | risk narrative, sizing suggestion (advisory only) | frontier | `[Risk]` |
| **Investor** | approved proposal | order plan: limit at mid, improvement steps, timeout | cheap | `[Investor]` |
| **Auditor** | fills, reconciliation, journal | daily journal, anomalies, lessons → skill notes | cheap | `[Auditor]` |

AutoHedge's `RISK_PROMPT` becomes *advisory narrative only*; sizing and limits are enforced by the gate. AutoHedge's stock-centric `QUANT_ANALYSIS_PROMPT` is replaced by an options schema (IV/HV, IVR, regime, PoP, EV after spread cost).

**Shared context (D16, E5.4).** Personas never hand results to each other in memory. Every producer (source job or persona) appends a typed entry to `context_entries` (`arc/context/`). Entry kinds are `raw_doc_ref`, `channel_brief`, `candidate`, `regime`, `shortlist`, `structures`, `risk_review`, `proposal`, `journal` and `note` (structured persona text: thesis, regime view, observation, risk flag, lesson, execution; D27); each is a pydantic model with `extra="forbid"`. Each job/step declares `reads` **and** `writes` in `config/routines.yaml`; an undeclared write fails the run (D27). Every run writes an append-only `run_manifests` row with its full metadata (D27). TTL and supersede policy come from the producer: `context_ttl` defaults in `config/routines.yaml`, with per-job `context:` overrides for the D14 source-profile TTLs.
- Writes are append-only, enforced by DB triggers. Superseding inserts a new row and flips the old row's status.
- Before a persona runs, the dispatcher takes a `ContextSnapshot` (active and unexpired at `as_of`, filtered by the kinds the persona `reads`). It records the snapshot id in `routine_runs.inputs_snapshot`, so any decision can be replayed against exactly what the persona saw.
- The prompt builders take that snapshot (`*_input_from_context`).
- The gate still receives only typed inputs from the runner and never queries the store.

### 2.5 Slack design

- **`#project-arc` (dev).** Every Kanban card gets one thread: creation post → worker progress comments → PR link → review verdict. Hermes' kanban notification subscriptions post into the same thread. Use `!cmd` prefix inside threads (Slack blocks slash commands there).
- **`#arc-investor` (trading).** One thread per trading day (`📅 2026-09-28 · session`). Persona posts are labelled `[Scout] [Director] [Quant] [Risk] [Investor] [Auditor]`. Proposal cards render as Hermes `clarify` → Block Kit **Approve / Reject** buttons; TTL default 20 min; expiry = reject. `!halt` in any thread trips the kill switch; only the owner can `!resume`.
- No order is ever submitted from `#project-arc`.

### 2.6 Hermes orchestration

- **Kanban**: board `project-arc`, project-bound → worktrees under `.worktrees/<id>/`, `--completion-contract mohitgulla/Project-Arc` → PR required; `review_dispatch: true` runs the review lane before `done`. `max_in_progress: 1`, `auto_decompose: false`.
- **Routines** (E5.4 dispatcher, E5.3 defaults, D16): one Hermes cron → `arc routines tick` every 5 min; cadences/chains live in `config/routines.yaml`. Defaults: **Scout at 22:00 ET and 12:00 ET (D15)**, `06:30 PT` pre-market scan, `every 30m 06:30–13:00 PT` intraday monitor, `13:30 PT` post-market reconcile + journal, weekly scorecard Friday.
  - **Tick algorithm.** Each tick:
    1. Expires context entries past their TTL.
    2. Plans the due slots in `(cursor, now]` for each job. Missed slots collapse into one catch-up run, which happens only while the slot's catch-up window (`ttl`, default 2h, or one `every` interval) is still open. Otherwise the slot is recorded as `skipped`.
    3. Runs source jobs before personas, so `after_sources` is satisfied.
    4. Runs each persona's `chain` under one `chain_run_id`.
    5. Fires `<job>.completed` triggers, whose `if:` expressions are evaluated safely over the run's metrics plus `session`.
    6. Drains queued external events (`arc routines emit approval|halt`).
  - **Idempotency.** `routine_runs` is unique on `(job, scheduled_for)` and on `(chain_run_id, step)`, so a doubled tick never re-runs a job. Retrying a failed chain resumes from its failed step.
  - **Locks and halt.** There is a flock per job, plus one global `llm` lock so personas run one at a time. `!halt` skips every persona except those marked `halt_exempt` (Auditor); sources keep fetching.
  - **Heartbeats.** Personas post one line to the #arc-investor day thread. Sources are quiet and get folded into the next persona line. Failures always alert.
  - **CLI.** `arc routines validate|list|tick [--dry-run]|run <job> [--chain]|history|emit`, and `arc context show|schemas|trace`.
- **MCP**: Alpaca MCP server v2 (`uvx alpaca-mcp-server`, `ALPACA_TOOLSETS` restricted to read-only in persona sessions); order submission goes through `arc.execution`, not MCP, in Phase 1.
- **Hooks**: `hermes/hooks/arc-gate` — `pre_tool_call`, matcher on broker order tools, fail-closed.
- **Sentinel** (D23): profile `arc-sentinel` (Fable 5.1, max effort), cron `arc-sentinel-daily-audit` at 14:00 PT → pre-run gate `~/.hermes/profiles/arc-sentinel/scripts/arc_sentinel.py` (SHA skip + deterministic checks) + skill `arc-sentinel`; state in `~/.hermes/profiles/arc-sentinel/sentinel/`; reports and owner commands (`create|comment|wontfix|accept S-<n>`, `rerun`) in the Sentinel thread in #project-arc.
- **Profiles**: single `default` profile is the worker in Phase 1 (assignee `default`). A dedicated `arc-worker` profile is an E8 item once provider auth for profiles is settled.

---

## 3. Phases

| Phase | Outcome | Exit criteria |
|---|---|---|
| **P0 Gate** | Plan approved | Owner completes card P0 → board releases |
| **1 Foundations** (E1) | repo, config, audit store, Alpaca adapter, Slack plumbing | integration test places + cancels a paper `mleg` order; thread-per-card works |
| **2 Engine** (E2, E3) | Greeks, structures, scanner, **gate + hook** | gate has 100% branch coverage; hook blocks an unsigned order in a live test |
| **3 Ingestion + Personas** (E4, E5) | Scout→Director→Quant→Risk pipeline, cron | dry-run produces a Proposal end-to-end with no broker call |
| **4 Approval + Execution** (E6) | Slack card → approval → paper order → fills → reconcile | first approved paper trade logged with full audit trail |
| **5 Evaluation** (E7) | cost-aware backtest, paper scorecard | 4 weeks of paper results + written go/no-go for anything further |
| **6 Ops** (E8) | model routing (fallback later), monitoring, control tower, local models (blocked on Mac Studio) | ongoing |

---

## 4. Work breakdown (Kanban cards)

IDs below are the card titles on the board. Dependencies are Kanban parent links (a card becomes `ready` only when its parents are `done`).

**P0** — Approve plan of record *(human; blocked until owner completes it)*

**E1 Foundations**
- E1.1 Repo scaffold — uv/3.12, `arc/` package, ruff, pytest, hypothesis, Makefile, CI, `docs/ARCHITECTURE.md` ← P0
- E1.2 Config, secrets, calendar — pydantic-settings, `ARC_ENV` hard switch, limits, universe, `exchange_calendars` ← E1.1
- E1.3 Audit store — SQLite schema + migrations + event-sourced order state machine ← E1.1
- E1.4 Alpaca paper adapter — `BrokerAdapter` protocol, `MarketDataProvider` protocol, chains via snapshots (Greeks), `mleg` orders, cancel/status; integration test on paper ← E1.2
- E1.5 Slack plumbing — thread-per-card, persona labels, message templates, `!halt` stub ← E1.1
- M1 Milestone — Foundations complete *(human)* ← E1.3, E1.4, E1.5

**E2 Pricing & Greeks (deterministic)**
- E2.1 Pricing + Greeks — py_vollib with QuantLib cross-check; Δ Γ ν Θ ρ Vanna Volga; property tests ← E1.1
- E2.2 Structure model — legs → payoff, max gain/loss, breakevens, net Greeks, margin estimate ← E2.1
- E2.3 Chain scanner (`arc chains`, D21) — liquidity filters, IVR/percentile, delta-targeted strikes 30–45 DTE ← E1.4, E2.2
- E2.4 Exit policy + managed-exit model — `config/exits.yaml`, MC managed PoP/Net EV vs static, `evaluate_position` (D23) ← E2.2

**E3 Risk Proxy Gate (non-bypassable)**
- E3.1 Gate rules engine — all limits from D4 as pure functions; 100% branch coverage ← E1.3, E2.2
- E3.2 Enforcement hook — Hermes `pre_tool_call` fail-closed + `GateToken` HMAC ← E3.1
- E3.3 Kill switch + daily halt — persisted halt state, `!halt`/`!resume` ← E3.1, E1.5
- E3.4 Account profiles + debit strategies — `margin | cash_debit | cash_long_only`, gate rule `account_profile`, paper default `cash_debit` (D25) ← E2.4

**E4 Ingestion (Scout)**
- E4.1 Source connectors — RSS, SEC EDGAR (10 req/s, UA header), earnings calendar, YouTube transcripts (yt-dlp) ← E1.2
- E4.1b YouTube audio-transcription fallback — local mlx-whisper when no captions (D15) ← E4.1
- E4.1c YouTube caption 429 handling — classify, per-run breaker, DB-persisted exponential cooldown (D15) ← E4.1b
- E4.2 Candidate pipeline — LLM summarization filter → `Candidate`, dedupe, storage, confidence threshold ← E4.1, E1.3
- E4.3 Regime features — Markov 3-state regime + IV/HV + IVR as structured inputs ← E1.4
- E4.4 Channel processors — per-channel profile + extraction guidelines → `ChannelBrief` (levels, directional calls, catalysts, risk flags, tickers; each with verbatim quote); active-brief lifecycle (superseded by next video); StockedUp first (D13, D14) ← E4.1, E4.2

**E5 Personas & orchestration**
- E5.1 Persona skills — six `SKILL.md` + JSON output schemas + prompt builders ← E1.1
- E5.2 Pipeline runner — candidate → structures → gate → proposal; idempotent, resumable, fully logged, dry-run mode ← E2.3, E3.1, E4.2, E5.1
- E5.3 Cron routines — install `arc routines tick` cron + default routines.yaml (Scout 22:00/12:00 ET, pre-market, intraday, post-market, weekly) ← E5.2, E5.4
- E5.4 Routine dispatcher + context store — per-source/per-persona cadence, chains, event triggers, `context_entries` + snapshots (D16) ← E1.3, E4.2
- E5.5 Persona digest cards — one Block Kit layout (`arc/slack/blocks.py`) for every persona post, `notify: card` knob (D22) ← E6.1
- E5.6 Persona I/O contracts — declared reads/writes (fail-closed), `note` kind, run manifest (all run metadata), schema registry, `arc context trace` (D27) ← E2.4, E5.3, E5.5, E7.4
- E5.7 Wider funnel — open universe + liquidity screen, Director ranks all with evidence, Quant/Risk process every upstream item (D28) ← E5.5, E5.6, E3.4

**E6 Approval & execution**
- E6.1 Slack proposal card — `#arc-investor` daily thread, Approve/Reject, TTL; D22 layout + per-persona decision trail ← E1.5, E5.2
- E6.1a Proposal card v2 — net EV + cost/liquidity breakdown, spot/moneyness, IV/IVR/IVP/HV, exit plan (D23) ← E6.1, E2.4
- E6.2 Execution — approved → limit `mleg` at mid with bounded improvement; fills; cancel on timeout; exits from E2.4's policy ← E6.1, E1.4, E3.2, E2.4
- E6.4 Position manager — early profit-taking + close-to-reallocate proposals (D19) ← E6.2, E5.4, E2.4, E5.6
- E6.3 Reconciliation — broker vs local positions, PnL snapshots, mismatch alerts ← E6.2

**E7 Backtest & evaluation**
- E7.1 Historical data — Alpaca options history (Feb 2024→) + ThetaData free EOD; storage ← E1.4
- E7.2 Cost-aware backtester — optopsy or in-house; walk-forward; baseline report for D4 structures ← E7.1, E2.2
- E7.3 Paper scorecard — metrics, weekly report to `#arc-investor` ← E6.3, E7.4
- E7.5 Ranking backtest — credit_width vs ev vs managed Net EV vs rorc_day per profile; pre-registered switch rule (D25) ← E2.4, E3.4, E7.2
- E7.4 Decision journal — every decision + reason_code + inputs in an append-only DB journal; outcome attribution; root-cause reviews; `arc journal show|gaps|replay` (D22) ← E6.1

**E8 Ops**
- E8.1 Per-persona model routing (fallback provider: later, note only) ← P0
- E8.2 Monitoring — heartbeat, gateway health, log rotation, alerts ← E5.3
- E8.3 Streamlit control tower over Tailscale ← E6.3
- E8.5 Slack control panel — registry, bounded owner-only overrides, audit/revert, effective config everywhere (D26) ← E3.4, E2.4
- E8.4 Local model path (Mac Studio) — blocked until hardware arrives (D3) ← E8.1

---

## 5. Risk defaults (gate config, Phase 1)

| Rule | Default | Source |
|---|---|---|
| Max allocation per underlying | 5% of equity (max loss basis for defined-risk) | brief §7 |
| Daily portfolio loss halt | 3.0% (realized + unrealized) | brief §7 |
| Spread/tick check | limit inside NBBO; spread ≤ 10% of mid or ≤ $0.10 | brief §7 + liquidity evidence |
| Wash-sale audit | no re-open within 30 days of a loss close, same underlying | brief §7 |
| Portfolio Greek caps | |net Δ| ≤ 0.30 × equity/100 per $; |ν| ≤ 0.5% equity per vol-pt (tune in E3.1) | brief §7 |
| Structure whitelist | vertical, iron condor, long call/put | D4 |
| DTE window | 30–45 entry; no 0DTE | D4 |
| Earnings blackout | no short premium through earnings unless Director flags "earnings play" and Risk concurs; still gated | evidence |
| Max open positions | 8 | default |
| Approval TTL | 20 min | default |

---

## 6. Missing tools / architectural requirements (suggested additions to the brief)

1. **Order state machine + idempotency keys** — brief had "SQLite logs"; we need event-sourced transitions and client order ids to survive crashes/retries.
2. **Gate token (HMAC) + fail-closed hook** — the brief's gate is "non-bypassable" in prose; this makes it non-bypassable in code.
3. **Kill switch / halt state** persisted outside the LLM loop.
4. **Reconciliation job** — broker is the source of truth; local state must be reconciled every session.
5. **Market calendar + clock discipline** — early closes, holidays, DTE math, time stops (QFX lesson).
6. **Cost-aware backtester** — evidence says spreads eat retail edge; no strategy ships without a cost model.
7. **Fallback LLM provider** — subscription rate limits already broke research fan-outs once.
8. **Data quality checks** — stale quotes, indicative vs OPRA, missing Greeks → proposal rejected.
9. **Secrets hygiene** — `.env` only, `ARC_ENV=live` credential file absent by construction in Phase 1.
10. **Tax-lot tracking** — needed for wash-sale audit and later Section 1256 (index options).
11. **Look-ahead guard for LLM backtests** — personas must not be evaluated on periods inside their training data without masking.
12. **Observability** — heartbeat to Slack, structured logs, run ids across Kanban → cron → Slack.

---

## 7. Evidence that shaped the plan

- Management rules (50% profit / 21 DTE) do not beat hold-to-expiry statistically over 163 SPY cycles; 21-DTE only caps tails for undefined-risk → defined-risk first, exits are config not dogma (theoptionsbench, 2026-09-22).
- Earnings IV−RV spread −16.5pp over 37,508 S&P 500 events (2010–2025), but not a retail edge after spreads → earnings plays are flagged, not default (theintrinsicinvestor).
- FINRA 26-10: PDT/$25k replaced by intraday margin standards (effective 2026-06-04, phase-in to 2027-10-20) → less constraint on paper→live sizing.
- Baseline-first research (Bawa): trade volume before selectivity — the backtester must emit a baseline before filters.
- QFX lessons: fees > 50% of returns at higher frequency; time stops; fewer parameters; know the broker's environment → E1.4 integration test and E7.2 cost model.
- Markov regime (Jackson): regime + stickiness gate structure choice (sideways → premium selling; signed signal → directional).
- LLM-trading failure modes (AlgoTrada, TradeTrap, look-ahead bias paper) → §6 items 2, 8, 11.

## 8. Costs (Phase 1)

Alpaca paper: $0 · Alpaca Basic data: $0 · ThetaData free EOD: $0 · Slack free: $0 · Claude subscription: existing · fallback provider: none for now (wire in later). Optional later: Alpaca Algo Trader Plus $99/mo, ThetaData Value $40/mo, Massive Options Starter $29/mo.

## 9. Open decisions (ask, don't assume)

All original items resolved on 2026-09-27:

1. ~~Fallback provider~~ → **D8**: Anthropic Subscription, no fallback for now (note to wire in later). Frontier = Opus 5.5, cheap = Opus 5.
2. ~~Universe~~ → **D9**: Confirmed 20-ticker list, configurable via Scout.
3. ~~Approval TTL & approvers~~ → **D10**: Owner-only + auto-approve mode for paper (`ARC_AUTO_APPROVE`). TTL stays at 20 min default.
4. ~~Alpaca paper API keys~~ → Stored in `~/.hermes/.env`. **⚠️ Keys were exposed in Slack — rotate them.**
5. ~~Push policy~~ → **D11**: Auto-push with completion contract.
