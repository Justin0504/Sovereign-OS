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

## Worker tool-use loop (agentic delivery)

`BaseWorker.run_with_tools(system, user, tool_handlers)` is a provider-agnostic
loop: the model emits JSON to either call a tool `{"action":"tool","tool":...,"args":{...}}`
or finish `{"action":"final","output":...}`. Tool observations are fed back, up
to `max_steps`, then a final answer is forced. This lets a worker gather REAL
information mid-task instead of answering from memory.

Wired (opt-in via `context['use_tools']`):
- **ResearchWorker / DataAnalysisWorker** → `web_fetch` (real current info).
- **CodeAssistantWorker** → `code_workspace` tools (`list_files`/`read_file`/`run_tests`),
  rooted at `context['workspace_root']` (the model cannot change the root).

Default (no `use_tools`) keeps the single-shot behavior. Enable it (and provide
`workspace_root` for code) to move research/data/coding deliverables toward 90/100:
the worker reads the repo or the web, then writes the answer grounded in it.

## Figma connector (design category)

`figma` reads a Figma design file via the REST API so a worker can implement,
extend, or audit it — `dispatch("figma", ref="<figma url or key>")` returns the
file name + an outline of its node tree (pages → frames → components → text).
Requires `FIGMA_TOKEN`.

Wired into **DesignBriefWorker**: when `context['use_tools']` is set and the brief
references a Figma URL (or `context['figma_file']` is given), the worker reads the
file with `read_figma` and designs against the real structure.

**Limitation:** Figma's REST API is read-only — agents can read a file but cannot
create or edit designs via REST (authoring needs Figma's in-app Plugin/Agent).
Canva is similar. So this grounds design work in an existing file; generating new
visuals would use an `image_gen` connector instead.

## image_gen + submit_pr

- **`image_gen`** renders a visual from a prompt (`dispatch("image_gen", prompt=...)`):
  provider-agnostic (injectable generator; default OpenAI Images via
  `IMAGE_GEN_API_KEY`/`OPENAI_API_KEY`), **dry-run with no key**. DesignBriefWorker
  offers `read_figma` + `generate_image` under `context['use_tools']`, so it can
  deliver an actual mockup/logo, not just a spec.
- **`submit_pr`** is the last step of a coding deliverable — branch, commit, push,
  and open a PR (`git` + `gh`). **Dry-run unless `SOVEREIGN_CODE_EXEC_ENABLED`**.
  With execution enabled, the CodeAssistantWorker's tool loop becomes the full
  bug-fix path: `read_file` → `run_tests` → `submit_pr`. This is what closes the
  gap toward 90/100 on real coding bounties (a tested PR, not just analysis).
  **Limitation:** there is no built-in sandbox — only enable execution against a
  trusted/sandboxed checkout (run the worker in a container/VM for untrusted repos).
