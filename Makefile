.PHONY: install test lint typecheck run

install:
	uv sync --group dev

test:
	uv run pytest -v

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy src

run:
	uv run uvicorn booking_agent.api.main:app --reload --port 8000

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests
