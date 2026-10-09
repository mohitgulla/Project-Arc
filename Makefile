.PHONY: check test test-gate lint fmt audit lock-check web web-check web-api web-e2e

check: lock-check lint fmt audit test test-gate

lock-check:
	uv lock --check

# Known-vulnerability scan of the locked environment (PyPI advisory DB).
# The project itself is not published on PyPI, so it is skipped, not audited.
audit:
	uv run pip-audit --skip-editable

lint:
	uv run ruff check arc/ tests/
	uv run lint-imports

fmt:
	uv run ruff format --check arc/ tests/

test:
	uv run pytest

test-gate:
	uv run pytest tests/test_gate.py tests/test_halt.py tests/test_gate_token.py \
		tests/test_gate_hook_policy.py tests/test_gate_band.py tests/test_account_profiles.py \
		tests/test_day_trades.py tests/test_gate_ticks.py tests/test_gate_live_size_cap.py \
		-v --tb=short \
		--cov=arc.gate --cov-branch --cov-report=term-missing --cov-fail-under=100

# ---------------------------------------------------------------------------
# Control tower v2 web app (E8.7, D35). Needs Node >= 22.12; `make check` does not.
# ---------------------------------------------------------------------------

# Build the SPA into arc/tower/static/ (served by `arc tower serve`).
web:
	cd web && npm ci && npm run build

# Lint, typecheck and unit tests (vitest) for the web app.
web-check:
	cd web && npm run lint && npm run typecheck && npm test

# Regenerate web/openapi.json and the typed client from the FastAPI app.
web-api:
	uv run python -m arc.tower.openapi web/openapi.json
	cd web && npm run gen:api

# Playwright: shell + /kitchen-sink at 390x844, 768x1024, 1440x900 in both themes, against
# `arc tower serve --local` on a scratch DB. Screenshots in web/e2e/screenshots/.
web-e2e: web
	cd web && npx playwright test
