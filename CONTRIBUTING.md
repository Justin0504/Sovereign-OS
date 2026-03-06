# Contributing to Sovereign-OS

Thank you for your interest in contributing. This document outlines how to get started.

## Development setup

```bash
git clone https://github.com/your-org/Sovereign-OS.git
cd Sovereign-OS
pip install -e ".[dev]"
```

- **Python:** 3.12+
- **Tests:** `pytest tests/ -v`
- **Lint:** Run your preferred linter (e.g. ruff, black) on `sovereign_os/` and `tests/`.

## Code style

- Use type hints where practical.
- Prefer Pydantic v2 for config and message passing.
- Keep async/await consistent: governance and agent calls are async.

## Submitting changes

1. **Fork** the repo and create a branch from `main`.
2. **Implement** your change; add or update tests if applicable.
3. **Run tests:** `pytest tests/ -v`
4. **Open a Pull Request** with a clear description. Use the PR template if present.
5. Ensure CI (GitHub Actions) passes.

## Areas to contribute

- **Charters:** Example YAML configs for different use cases (see `charter.example.yaml`, `docs/CHARTER.md`).
- **Workers:** New `BaseWorker` implementations and registry wiring (see `docs/WORKER.md`, `sovereign_os/agents/`).
- **Phase 6b:** On-chain settlement or sovereign identity stubs (see `docs/PHASE6.md`).
- **Docs:** Fixes and improvements to `docs/`, README, and docstrings.
- **Tests:** More unit and integration tests, especially for edge cases.

## Reporting issues

Use GitHub Issues. For bugs, include:

- OS and Python version
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs or tracebacks

For feature requests, describe the use case and how it fits the Charter-driven, governance-first design.

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (see repository root).
