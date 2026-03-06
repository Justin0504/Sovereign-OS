# Good first issues

Contributions are welcome. These areas are good for a first PR (documentation, tests, or small features). On GitHub, consider labeling such issues with **`good first issue`**.

## Documentation

- Add or improve docstrings for a module in `sovereign_os/`.
- Translate a section of README or QUICKSTART into another language (e.g. Chinese summary).
- Add a “Common pitfalls” or FAQ section to QUICKSTART or CONFIG.
- Document a charter template (e.g. in `charters/`) with a short example.

## Examples and DX

- Add a minimal **ingest mock server** (e.g. Flask/FastAPI) that returns `examples/ingest_example.json` for local testing of `SOVEREIGN_INGEST_URL`.
- Add a **backup script** (e.g. `scripts/backup.sh`) that wraps the steps in [BACKUP.md](BACKUP.md).
- Add **more example charters** (e.g. for a specific use case) under `charters/`.

## Tests

- Add **unit tests** for a built-in worker (e.g. `AssistantChatWorker`, `CodeReviewWorker`) with a mocked LLM.
- Add **tests** for `POST /api/jobs/batch` (validation, response shape).
- Add a **recovery test** that creates a job, simulates restart (reload store), and asserts the job is still present.

## Code quality

- Add **type hints** to a function or module that lacks them.
- Refactor a long function into smaller helpers and add a brief test.

When in doubt, open an issue to discuss the approach before a large PR.
