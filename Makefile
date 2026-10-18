.PHONY: check test test-rest test-gate test-slow lint fmt audit lock-check web web-check web-api web-e2e

check: lock-check lint fmt audit test

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

# Tests run in parallel (pytest-xdist): one worker per CPU. Override for a serial run
# with `make test PYTEST_WORKERS=0`. A bare `uv run pytest` stays serial.
PYTEST_WORKERS ?= auto

# The risk-gate suite. Its 100% branch coverage of arc.gate must come from these files
# alone, so they run once, in `test-gate`, and `test-rest` skips them.
GATE_TESTS := tests/test_gate.py tests/test_halt.py tests/test_gate_token.py \
	tests/test_gate_hook_policy.py tests/test_gate_band.py tests/test_account_profiles.py \
	tests/test_day_trades.py tests/test_gate_ticks.py tests/test_gate_live_size_cap.py \
	tests/test_gate_sleeves.py tests/test_gate_caps.py tests/test_gate_delta.py \
	tests/test_gate_regime.py tests/test_gate_open_risk.py tests/test_gate_cluster.py

# Full suite: every test runs exactly once.
test: test-rest test-gate test-slow

# Tests marked `serial` assert a wall-clock budget, which CPU contention from parallel
# workers can break, so they run alone after the parallel pass.
# CI (D134) runs test-rest split over parallel jobs via ARC_TEST_SHARD=<i>/<n>
# (tests/ci_shard.py); unset, it runs everything.
test-rest:
	uv run pytest -n $(PYTEST_WORKERS) -m "not serial and not slow" $(addprefix --ignore=,$(GATE_TESTS))
	uv run pytest -m "serial and not slow" $(addprefix --ignore=,$(GATE_TESTS))

# Seeded Monte Carlo simulations (`slow` marker, E25.7/E25.9). Own CI job (D134).
test-slow:
	uv run pytest -n $(PYTEST_WORKERS) -m "slow and not serial" $(addprefix --ignore=,$(GATE_TESTS))

test-gate:
	uv run pytest -n $(PYTEST_WORKERS) $(GATE_TESTS) \
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
