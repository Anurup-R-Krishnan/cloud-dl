# Contributing to cloud-dl

Thank you for helping improve `cloud-dl`.

## Development Setup

```bash
git clone https://github.com/Anurup-R-Krishnan/cloud-dl.git
cd cloud-dl
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pre-commit install
```

## Verification Before Opening a Pull Request

All pull requests must pass:

1. **Linting and formatting**:
   ```bash
   ruff check src/ tests/ regression_tests/ scripts/cloud-dl
   ruff format --check src/ tests/ regression_tests/
   ```
2. **Offline test suite**:
   ```bash
   pytest tests/ -v
   ```
3. **Secret hygiene**: Ensure no real instance IDs, public IP addresses, personal paths, or tokens appear in code, tests, or documentation.

## Commit Style

Use concise conventional commits (e.g., `feat(sync): ...`, `fix(ssh): ...`, `docs: ...`, `test: ...`).
