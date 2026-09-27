# Architecture — Project Arc

## System DAG

```
 Market Data & Math Engine [Deterministic]   Information Retrieval [Scout persona]
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
                              Trade Execution [Execution persona -> BrokerAdapter.alpaca_paper]
                                                        v
                              Auditor persona, SQLite audit, reconciliation --> back to Aggregator
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
|   +-- cli.py                  arc scan|propose|gate|approve|execute|reconcile|report
|   +-- config.py               pydantic-settings; ARC_ENV; limits; universe
|   +-- calendar.py             exchange_calendars: sessions, early closes, DTE
|   +-- models.py               pydantic v2 data contracts (see below)
|   +-- store/                  SQLite schema + migrations + repositories
|   +-- data/                   MarketDataProvider protocol; alpaca, thetadata
|   +-- pricing/                BS + Greeks (py_vollib, QuantLib cross-check)
|   +-- structures/             legs -> payoff, max gain/loss, breakevens, net Greeks
|   +-- scanner/                chain filters, IVR, delta-targeted strikes
|   +-- ingest/                 RSS, EDGAR, earnings, YouTube -> Candidate
|   +-- features/               regime (Markov 3-state), IV/HV, IVR
|   +-- personas/               JSON schemas + prompt builders (no side effects)
|   +-- gate/                   rules.py (pure), token.py, halt.py
|   +-- approvals/              Slack proposal card, TTL, ApprovalRecord
|   +-- execution/              order state machine, submit(), fills, exits
|   +-- broker/                 BrokerAdapter protocol; alpaca_paper.py
|   +-- reconcile/              broker vs local, PnL snapshots, alerts
|   +-- backtest/               cost-aware engine, walk-forward, reports
+-- tests/
+-- hermes/
    +-- skills/arc-*/SKILL.md
    +-- hooks/arc-gate/
    +-- routines/
```

## Data contracts (pydantic v2)

All models live in `arc/models.py`. See PLAN.md section 2.3 for full specifications.

### Candidate
Surfaced by the Scout persona. Fields: ticker, stance, catalyst_type (enum),
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
