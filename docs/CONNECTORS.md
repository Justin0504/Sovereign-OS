# Connectors

Each delivery category declares the tools it needs (`agents/categories.py`); the
connector registry (`connectors/registry.py`) catalogs them, reports readiness,
and dispatches the built-in ones. MCP-kind connectors are fulfilled via the
existing self-hiring path (`mcp-{skill}` worker + MCPToolGraph).

Inspect: `sovereign connectors` (CLI) or `GET /api/connectors` (web).

## Built-in connectors (real handlers via `connectors.dispatch(name, **kw)`)

| Connector | What it does | Notes |
|---|---|---|
| `send_email` | Send an email over SMTP | Dry-run unless `SOVEREIGN_EMAIL_LIVE=true` |
| `web_fetch` | GET a URL → readable text (HTML stripped) | http/https only; size+time capped. Gives research/data workers current info. |
| `code_workspace` | Read a code checkout (`list_files`/`read_file`) + guarded `run_tests` | Read is path-escape-guarded; gives coding workers repo context. |

```python
from sovereign_os.connectors import dispatch
dispatch("web_fetch", url="https://example.com")
dispatch("read_file", root="/path/to/repo", relpath="src/app.py")
dispatch("run_tests", root="/path/to/repo", action="run_tests")  # dry-run unless enabled
```

## Code execution — opt-in & limitations

`code_workspace.run_tests` executes a command only when
`SOVEREIGN_CODE_EXEC_ENABLED=true`; otherwise it returns a dry-run no-op. Reads
are always confined to the provided `root` (paths that escape are refused) and
capped in size. **Arbitrary code execution is the operator's decision** — enable
it only against a trusted/sandboxed checkout. There is no built-in sandbox; for
untrusted repos, run the worker process inside a container or VM.

This is what lets coding workers move from "analysis only" toward real bug-fix
delivery (read the repo → propose a fix → run the tests).
