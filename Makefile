.PHONY: check test test-gate lint fmt

check: lint fmt test test-gate

lint:
	uv run ruff check arc/ tests/
	uv run lint-imports

fmt:
	uv run ruff format --check arc/ tests/

test:
	uv run pytest

test-gate:
	uv run pytest tests/test_gate.py tests/test_gate_token.py tests/test_gate_hook_policy.py \
		-v --tb=short \
		--cov=arc.gate --cov-branch --cov-report=term-missing --cov-fail-under=100
