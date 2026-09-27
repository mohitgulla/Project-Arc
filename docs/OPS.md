# Project Arc — Operations Guide

**Status:** v0.1 · 2026-09-27 · maintained by: Hermes workers (E8 cards)

---

## 1. LLM Provider Configuration

### 1.1 Primary provider

| Setting | Value |
|---------|-------|
| Provider | Anthropic (subscription) |
| Model | `claude-opus-4.6` (via `claude-subscription-directsdk-experimental` plugin) |
| Auth | `CLAUDE_CODE_OAUTH_TOKEN` in `~/.hermes/.env` |
| Hermes config | `model.default: anthropic/claude-opus-4.6`, `model.provider: auto` |

Decision D8 (PLAN.md §0): Anthropic Subscription (Claude Opus 5.5) for all
implementation/execution tasks.

### 1.2 Fallback provider

**Status: NOT CONFIGURED — awaiting owner decision.**

Decision D8 deferred fallback to E8.1. The owner must choose a fallback
provider before this section can be completed. Options considered:

| Option | Cost | Pros | Cons |
|--------|------|------|------|
| OpenRouter (pay-as-you-go) | ~$15/1M tok (Sonnet) | 200+ models, single key, auto-routing | Adds a billing account |
| Nous Portal (OAuth) | Free tier / subscription | Free, built-in to Hermes | Model availability varies |
| Google Gemini (API key) | Free tier available | Large context, fast | Different tool-calling behavior |
| DeepSeek | ~$2/1M tok | Cheap, strong coding | China-hosted, latency |

Once decided, configure in `~/.hermes/config.yaml`:

```yaml
fallback_providers:
  - provider: <chosen-provider>
    model: <chosen-model>
```

Verify with: `hermes fallback list`

### 1.3 Auxiliary task providers

Hermes resolves auxiliary tasks (vision, compression, title generation) via
auto-detection. No custom auxiliary configuration is needed unless the primary
provider hits capacity. The auto-detection chain is:

    Main provider → fallback_providers → OpenRouter → Nous Portal → give up

---

## 2. Per-Persona Model Tiers

PLAN.md §2.4 defines two model tiers for Arc personas:

| Tier | Personas | Model | Rationale |
|------|----------|-------|-----------|
| **Frontier** | Director, Quant, Risk | `claude-opus-4.6` (primary) | Complex reasoning, multi-step analysis, structured output |
| **Cheap** | Scout, Execution, Auditor | TBD (fallback provider model) | High-volume, simpler tasks, cost optimization |

### 2.1 Routing mechanism

Per-persona model routing is implemented via Hermes `kanban_create` model
overrides. When the pipeline runner (E5.2) spawns persona tasks, it sets:

```python
# Frontier tier (Director, Quant, Risk) — use primary provider
kanban_create(
    title="...",
    assignee="default",
    # model and provider omitted → uses profile default (claude-opus-4.6)
)

# Cheap tier (Scout, Execution, Auditor) — use fallback/cheap model
kanban_create(
    title="...",
    assignee="default",
    model="<cheap-model>",          # e.g. "anthropic/claude-sonnet-4" or fallback model
    provider="<cheap-provider>",    # e.g. "anthropic" or fallback provider
)
```

### 2.2 Persona skill model hints

Each persona skill (E5.1) will declare its model tier in the SKILL.md
frontmatter metadata, so the pipeline runner can resolve the correct model:

```yaml
# hermes/skills/arc-scout/SKILL.md frontmatter
metadata:
  arc:
    persona: scout
    model_tier: cheap    # or "frontier"
```

### 2.3 Cost estimation (per pipeline run)

A single pipeline run (scan → propose → gate) invokes roughly:

| Persona | Calls/run | Tokens/call (est.) | Tier |
|---------|-----------|-------------------|------|
| Scout | 1-5 | ~2K in / ~1K out | cheap |
| Director | 1 | ~4K in / ~2K out | frontier |
| Quant | 1-3 | ~4K in / ~3K out | frontier |
| Risk | 1 | ~3K in / ~2K out | frontier |
| Execution | 0-2 | ~1K in / ~500 out | cheap |
| Auditor | 1 | ~2K in / ~1K out | cheap |

With the primary provider (Anthropic subscription), all tiers use the
subscription quota. Once a fallback/cheap provider is configured, cheap-tier
personas route there to conserve subscription capacity for frontier tasks.

### 2.4 Delegation model override

For subagent delegation within a session, Hermes supports a global cheap-model
pin in `config.yaml`:

```yaml
delegation:
  model: "<cheap-model>"       # all subagents use this unless overridden per-card
  provider: "<cheap-provider>"
```

This is useful for Scout fan-outs (E4) where multiple parallel subagents
process RSS/EDGAR/earnings sources.

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
| `<FALLBACK_PROVIDER_KEY>` | Fallback LLM provider | **No — pending decision** |

### 3.2 Environment switch

`ARC_ENV` defaults to `paper`. The `live` credential file does not exist.
Never set `ARC_ENV=live` in Phase 1.

---

## 4. Hermes Configuration Summary

### 4.1 Active config (`~/.hermes/config.yaml`)

| Section | Key | Value |
|---------|-----|-------|
| `model.default` | Primary model | `anthropic/claude-opus-4.6` |
| `model.provider` | Provider resolution | `auto` (resolves to Anthropic) |
| `kanban.review_dispatch` | Auto-dispatch reviewers | `true` |
| `fallback_providers` | Fallback chain | **Not configured** |
| `delegation.max_iterations` | Subagent turn cap | `250` |

### 4.2 Profiles

Phase 1 uses a single `default` profile. A dedicated `arc-worker` profile is
deferred (PLAN.md §2.6). Per-persona routing uses model overrides on kanban
cards, not separate profiles.

---

## 5. Monitoring (E8.2)

Placeholder — populated by card E8.2.

## 6. Local Models (E8.4)

Placeholder — populated by card E8.4 when the 128 GB Mac Studio arrives.
See PLAN.md §2.4 for target: Scout/Execution/Auditor tiers routed locally
via llama.cpp or omlx server.
