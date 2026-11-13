.PHONY: check test build

check:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked python -m unittest discover -s tests

test:
	uv run --locked python -m unittest discover -s tests

build:
	uv build --no-sources
