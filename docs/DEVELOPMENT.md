# PyHakoCLI Developer Guide

This guide is for contributors and developers who want to build, test, or modify PyHakoCLI from source.

## Prerequisites
- Python 3.9+
- `uv` (modern Python package manager)

## Installation (Source)

```bash
# Clone the repository
git clone https://github.com/xebjhm/PyHakoCLI.git
cd PyHakoCLI

# Install dependencies and sync environment
uv sync

# Verify installation
uv run pyhako-cli --help
```

## Development Cycle

### 1. Running Locally
Use `uv run` to execute the CLI within the managed environment:
```bash
uv run pyhako-cli --interactive
```

### 2. Running Tests
We use `pytest` for unit and integration testing:
```bash
uv run pytest
```

### 3. Local Build Testing (Shift-Left)
Test PyInstaller builds locally before pushing to CI:

**Build the executable**:
```bash
uv run python scripts/build_local.py
```

**Smoke test**:
```bash
uv run python scripts/test_build.py
# Or test directly:
./dist/pyhako-cli-linux --help
```

**Windows Helper**:
Run `scripts/build_windows.bat` (works from Explorer, CMD, or WSL path).

### 4. Linting & Formatting
We enforce strict code style using `ruff`:
```bash
uvx ruff check .
uvx ruff format .
```

## Contributing
We follow the [PyHako Core Contribution Guidelines](https://github.com/xebjhm/PyHako).
- **Versioning**: Semantic Versioning.
- **Code Style**: Ruff strict.
- **Commits**: Conventional Commits.
