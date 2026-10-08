set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Install dependencies
install:
    uv sync --extra dev

# Install the locked dependencies (CI)
ci-install:
    uv sync --locked --extra dev

# Format and fix lint
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# The gate every change passes: format, lint, types, tests
check:
    uv run ruff format --check .
    uv run ruff check .
    uv run pyright
    uv run pytest

# Run tests
test:
    uv run pytest

# Run the extension locally against ./config.json
dev:
    ZELOS_CONFIG_PATH=./config.json uv run python main.py

# Package for the Zelos marketplace; PYTHONDONTWRITEBYTECODE keeps the action dump's bytecode out
package:
    PYTHONDONTWRITEBYTECODE=1 zelos extensions package .

# Remove build artifacts
clean:
    rm -rf dist build .pytest_cache .ruff_cache .hypothesis *.tar.gz actions.json
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
