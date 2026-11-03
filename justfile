# Format the code
fmt:
    uv run ruff format
    uv run ruff check --fix

# Lint and type-check
lint:
    uv run ruff format --check
    uv run ruff check
    uv run pyrefly check

# Host-only unit tests of the harness
test:
    uv run pytest

# Run the scenarios; pass options through, e.g. `just run --hdc-target X navigation/`
run *ARGS:
    uv run servo-embedder-tests run {{ARGS}}

build:
    uv build
