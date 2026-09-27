.PHONY: check test test-gate lint fmt

check: lint fmt test

lint:
	uv run ruff check arc/ tests/

fmt:
	uv run ruff format --check arc/ tests/

test:
	uv run pytest

test-gate:
	uv run pytest tests/test_gate.py -v --tb=short
