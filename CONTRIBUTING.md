# Contributing to pysaka CLI

Thank you for your interest in contributing to pysaka CLI! This document outlines our development workflow, branching strategy, and contribution guidelines.

## Table of Contents

- [Git Flow Strategy](#git-flow-strategy)
- [Branch Naming Conventions](#branch-naming-conventions)
- [Development Setup](#development-setup)
- [The Changelog Rule](#the-changelog-rule)
- [Commit Message Guidelines](#commit-message-guidelines)
- [Building & Testing](#building--testing)
- [Pull Request Process](#pull-request-process)

---

## Git Flow Strategy

We follow a modified Git Flow branching model with `dev` as our integration branch.

```
main ─────●─────────────────────●─────────────────●──────► Production
           \                   /                 /
            \    release/v1.0 /                 /
             \       ●───────●                 /
              \     /         \               /
dev ───────────●───●───●───●───●───●───●───●───●──────► Integration
                \     /   \     /       \   /
                 feat/a    feat/b        hotfix/x
```

### Branch Types

| Branch | Purpose | Branch From | Merge To |
|--------|---------|-------------|----------|
| `main` | Production-ready code. Strictly versioned. Tagged releases only. | - | - |
| `dev` | Main integration branch for ongoing development. | `main` (initial) | - |
| `feat/<name>` | New features and enhancements | `dev` | `dev` via PR |
| `fix/<name>` | Bug fixes (non-urgent) | `dev` | `dev` via PR |
| `release/vX.Y.Z` | Release preparation and stabilization | `dev` | `main` AND `dev` |
| `hotfix/<name>` | Urgent production fixes | `main` | `main` AND `dev` |

---

## Branch Naming Conventions

Use lowercase with hyphens. Be descriptive but concise.

```
feat/blog-backup
feat/parallel-download
fix/auth-refresh
release/v1.2.0
hotfix/crash-on-startup
```

**Prefixes:**
- `feat/` - New features
- `fix/` - Bug fixes
- `refactor/` - Code refactoring (no behavior change)
- `docs/` - Documentation only
- `test/` - Test additions or fixes
- `build/` - Build system or CI changes
- `release/` - Release preparation
- `hotfix/` - Urgent production fixes

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/xebjhm/saka-cli.git
   cd saka-cli
   ```

2. **Install dependencies** using `uv`:
   ```bash
   uv sync
   ```

3. **Run the CLI** (development mode):
   ```bash
   uv run python -m saka_cli
   ```

4. **Run Tests**:
   ```bash
   uv run pytest
   ```

---

## The Changelog Rule

> **Every Pull Request MUST update the `[Unreleased]` section of `CHANGELOG.md`.**

This is non-negotiable. The changelog is our historical record and release notes source.

### How to Update the Changelog

1. Open `CHANGELOG.md`
2. Find the `## [Unreleased]` section
3. Add your change under the appropriate category:

```markdown
## [Unreleased]

### Added
- New feature description (#PR-number)

### Changed
- Modified behavior description (#PR-number)

### Fixed
- Bug fix description (#PR-number)
```

---

## Commit Message Guidelines

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification.

### Format

```
<type>(<scope>): <description>
```

### Types

| Type | Description |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Code refactoring |
| `test` | Adding or updating tests |
| `build` | Build system or dependencies |
| `ci` | CI/CD configuration |
| `chore` | Maintenance tasks |

### Examples

```bash
feat: add blog backup command
fix(auth): handle token refresh failure gracefully
build: update PyInstaller spec for Windows
docs: update CLI usage examples
```

---

## Building & Testing

### Local Development

Run the CLI directly:
```bash
uv run python -m saka_cli --help
uv run python -m saka_cli -s hinatazaka46
```

### Build Executable

Build with PyInstaller:
```bash
uv run python scripts/build_local.py
```

### Test the Build

Smoke test the executable:
```bash
uv run python scripts/test_build.py
# Or directly:
./dist/saka-cli --help
```

### Run All Tests

```bash
uv run pytest -v
uv run ruff check .
uv run mypy .
```

---

## Pull Request Process

### Before Creating a PR

1. **Update your branch** with latest `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout your-branch
   git rebase dev
   ```

2. **Run all checks**:
   ```bash
   uv run pytest -v
   uv run ruff check .
   ```

3. **Test the build** (for CLI changes):
   ```bash
   uv run python scripts/build_local.py
   uv run python scripts/test_build.py
   ```

4. **Update CHANGELOG.md** under `[Unreleased]`

### PR Requirements

- [ ] Branch is up to date with `dev`
- [ ] All tests pass
- [ ] Linting passes (`ruff`)
- [ ] Build works (for CLI changes)
- [ ] CHANGELOG.md is updated

---

## Code Quality Standards

- **Formatter**: `ruff format`
- **Linter**: `ruff check`
- **Type Hints**: Required for public functions
- **CLI UX**: Clear error messages, helpful `--help` output

---

## License

By contributing, you agree that your contributions will be licensed under its MIT License.
