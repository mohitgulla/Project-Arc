# Project Arc — agent conventions

You are working a Kanban card for Project Arc, an agentic **options** trading system. Read `docs/PLAN.md` first; it is the plan of record. Card titles map to §4 of that document.

## Hard rules
- **Paper only.** `ARC_ENV` defaults to `paper`. Never add live credentials, never write code that reads a live credential file.
- **The risk gate is deterministic.** Nothing under `arc/gate/` may import an LLM client, call a network, or read prompt text. Pure functions, 100% branch coverage (`make test-gate`).
- **Personas never call the broker.** Only `arc.execution.submit()` may submit an order and it requires a `GateToken` and an `ApprovalRecord` for the same payload hash.
- **Secrets** live in `~/.hermes/.env` only. Never commit `.env`, keys, or account ids.
- **Do not assume key decisions.** If a card needs a decision not covered by `docs/PLAN.md §0/§5`, block the card with `kanban_block` and state the question; do not pick a default silently.

## Toolchain
- Python 3.12 via uv: `uv sync`, `uv run pytest`, `uv run ruff check --fix`, `uv run ruff format`.
- Tests are required for every card. Property tests (hypothesis) for pricing and gate math.
- `make check` must pass before you mark a card complete.
- **Green main.** Never merge on a red main; the PR head's `check` job must be green. Tests inject `now`; they never read the wall clock.

## Git / PR
- Work in the worktree Hermes gave you. Branch name is preset. Commit small, message format: `E1.2: <what changed>`.
- Open a PR against `main` and pass its URL as `metadata.published_pr` on completion (completion contract).
- Never force-push, never merge your own PR.
- **Strategy lane (D44, CI job `strategy-lane`, docs/OPS.md §5.20).** A PR touching a strategy path (`config/strategy_lane.yaml`) must carry one line in its body: `Experiment: XP-<n>` (registered experiment), `Flag: <stem>.<key>` (a NEW key in `config/<stem>.yaml` defaulting off), or `Lane: fast — <reason>` (bug/safety/infra; arc-sentinel audits). Flipping an existing strategy default needs `Experiment: XP-<n>` with a committed `win` verdict (`config/experiments/live/verdicts/XP-<n>.yaml`), and only to the values it tested.

## Comms
- Progress, blockers, and the PR link go in the card's thread in `#project-arc` (Hermes posts card events there).
- Nothing related to a card is posted in `#arc-investor`.

## Style
- Typed Python, pydantic v2 models for every data contract in `docs/PLAN.md §2.3`.
- No `print`; use `structlog`. No global mutable state. Time is always `datetime` with tz `America/New_York` via `arc.utils.calendar`.
