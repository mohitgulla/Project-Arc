---
name: arc-scalp
description: "Arc Scalp (Information Retrieval) persona"
---

# Scalp (Information Retrieval)

**Slack label:** [Scalp]
**Model tier:** cheap

## Role

Scan raw information feeds (RSS, EDGAR, earnings calendars, YouTube transcripts) and surface trading candidates for the configured universe.

## Inputs

Raw feeds from RSS, SEC EDGAR, earnings calendars, YouTube transcripts; configured ticker universe.

## Output schema (strict JSON)

ScalpOutput (see arc/personas/schemas.py): candidates[] with ticker, stance, catalyst_type, catalyst_date, confidence, sources, rationale; plus scan_summary.

All output MUST be valid JSON matching the schema. No prose outside the JSON object.

## Forbidden actions

- Do NOT call any broker API or place any orders.
- Do NOT suggest position sizes or contract counts.
- Do NOT access any tools beyond information sources.

## Prompt builder

`arc.personas.builders.build_scalp_prompt()` — pure function, no side effects, no network calls.

## Pipeline (E4.2)

`arc.ingest.scalp.run_scalp()` batches unscalped `raw_docs` into this prompt and runs it through
Hermes one-shot (`hermes -z`, cheap tier: model from `config/llm_routing.yaml`, default
`anthropic/claude-opus-5`, `--ignore-rules`, inert toolset). The reply is then filtered with no LLM involved:

- schema: must validate as `ScalpCandidateOut`
- ticker must be in `ARC_UNIVERSE`
- confidence must be at least `ARC_SCALP_MIN_CONFIDENCE` (default 0.6)
- sources: only URLs of docs in the batch are kept; no grounded source means the candidate is dropped
- merged per ticker per ET day: sources unioned; same stance keeps the max confidence; opposing
  stances subtract; a tie becomes neutral with confidence 0

Funnel discipline: downstream stages read only `candidates_for_scanner()`, which returns typed
`Candidate` models. Rationale, scan summary and the verbatim reply stay in `scalp_batches` for
audit.

Dry run: `arc scan --dry-run` uses fixture docs and canned replies in `arc/ingest/fixtures/scalp/`
(no network).

## Channel briefs (E4.4)

Each YouTube channel has one processor: `arc/ingest/channels/<slug>/profile.yaml` +
`GUIDELINES.md` (+ `fixtures/`). Shared code lives in `arc/ingest/channels/base.py`. Adding a
channel is a new directory; no code change. Unknown channels fall back to `channels/default/`.
The newest transcript becomes a strict `ChannelBrief` (`arc/models.py`, `extra="forbid"`):
market_bias, levels, calls, catalysts, risk_flags, tickers_mentioned. It uses the same Hermes
one-shot backend as the pipeline above (`HermesScalpLLM`); there is no second client.

Code enforces these rules, independent of the LLM:
- sponsor/ad sentences (profile `sponsor_patterns`) are stripped before the prompt is built
- quote grounding: every item's `quote` (<=240 chars) must appear verbatim in the normalized
  transcript, or the item is dropped and logged as `channel.brief.drop`
- levels must be within +-25% of the reference price (quote mid, or the last close off-hours).
  With no price available they are kept with `unverified_price=true`
- tickers are normalized (S&P / "the market" -> SPY, Nasdaq -> QQQ, Dow -> DIA, Russell -> IWM).
  Non-universe tickers stay in the brief but never become Candidates (logged as proposed
  universe additions, D9)
- no sizing/order fields exist in the schema

Lifecycle (`channel_briefs` table, migration 004): the profile sets `ttl_sessions`,
`supersede: latest|accumulate` and `source_kind`. `expires_at` is the close of the Nth trading
session from `applies_to_session`, counted with `arc.utils.calendar`. StockedUp: 1 session,
`latest`, so a Sunday-night video informs Monday only and the next video supersedes it. Macro
sources use a long TTL with `accumulate`. Downstream reads `active_briefs(now)`.
`brief_to_candidates()` deterministically maps universe calls and ticker catalysts to
`Candidate` (confidence = conviction x trust_weight).

CLI: `arc ingest youtube --process [--dry-run] [--no-prices]`, `arc brief show [--channel <slug>]`.
Scheduling belongs to E5.3; the audio fallback for caption-less videos belongs to E4.1b.
