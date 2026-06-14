_default:
    @just --list

# Clean recipes

# Remove C++ and Rust benchmark build artifacts (fast to rebuild)
clean:
    rm -rf implementations/cpp/*/build
    rm -rf target

# Remove vendored library build and install trees (slow to rebuild — takes many minutes)
clean-vendor:
    rm -rf vendor/build
    rm -rf vendor/install
    # nasm is built in-tree (no out-of-tree support); wipe its build artifacts too.
    git -C vendor/nasm clean -fdx 2>/dev/null || true

# Remove all build artifacts
clean-all: clean clean-vendor

# Fix recipes (modify files in place)

fmt-cpp:
    #!/usr/bin/env bash
    set -euo pipefail
    files=$(git ls-files -- '*.cpp' '*.cc' '*.cxx' '*.h' '*.hpp' '*.hxx')
    if [ -n "$files" ]; then
        echo "$files" | xargs -n 10 -P "$(nproc)" clang-format -i -style=file
    fi

fmt-python:
    uv run ruff format .

fmt-rust:
    cargo fmt

fmt: fmt-cpp fmt-python fmt-rust

lint-python:
    uv run ruff check --fix .

lint-rust:
    cargo clippy --fix --allow-dirty --allow-staged -- -D warnings

lint: lint-python lint-rust

fix: fmt lint

# Check recipes (read-only, for CI)

check-fmt-cpp:
    #!/usr/bin/env bash
    set -euo pipefail
    files=$(git ls-files -- '*.cpp' '*.cc' '*.cxx' '*.h' '*.hpp' '*.hxx')
    if [ -n "$files" ]; then
        echo "$files" | xargs -n 10 -P "$(nproc)" clang-format --dry-run -Werror -style=file
    fi

check-fmt-python:
    uv run ruff format --check .

check-fmt-rust:
    cargo fmt --check

check-fmt: check-fmt-cpp check-fmt-python check-fmt-rust

check-lint-python:
    uv run ruff check .

check-lint-rust:
    cargo clippy -- -D warnings

check-lint: check-lint-python check-lint-rust

check: check-fmt check-lint

# Test recipes

test-rust:
    cargo test

test-python:
    uv run python scripts/test_plot_generation.py

test: test-rust test-python

# Pre-commit hook recipe

pre-commit:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "Running formatters..."
    just fmt

    echo "Running Python linter..."
    just lint-python

    echo "Running Rust clippy (with fix)..."
    cargo clippy --fix --allow-dirty --allow-staged -- -D warnings

    echo "Checking for modified files..."
    if ! git diff --exit-code; then
        echo ""
        echo "ERROR: Files were modified by formatters/linters."
        echo "Please re-stage the changes and try again:"
        echo "  git add -u && git commit"
        exit 1
    fi

    echo "Running Rust clippy (final check)..."
    cargo clippy -- -D warnings

    echo "Pre-commit checks passed."
