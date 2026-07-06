# Agent Backends & Compatibility

Sovereign-OS runs complex tasks through a pluggable **AgentBackend** so a worker's
"brain" can be a plain LLM *or* a purpose-built coding agent (Claude Code, OpenAI
Codex, Gemini CLI, Aider, …) — chosen by **config, not code**. This is the
compatibility + future-proofing layer: one stable interface, many backends, and new
agents plug in as a command template.

## Why

For multi-file coding, refactors, and running a test suite, a dedicated coding agent
delivers far better than a single chat call. Rather than hard-wire each vendor,
Sovereign-OS routes the whole task to an external agent that does its own tool use
inside the workspace, then returns a result — governed by the same budget, audit, and
trust machinery as any worker.

## Selecting a backend

```bash
# Global default for every skill
SOVEREIGN_AGENT_BACKEND=claude-code        # native (default) | claude-code | codex | gemini-cli | aider

# Per-category override (wins over global). Category keys: coding, research, data, ...
SOVEREIGN_BACKEND_CODING=codex

# Actually spawn the agent (running it executes code/tools on the host).
# DRY-RUN until this is set — unset = the agent is described but never run.
SOVEREIGN_AGENT_BACKEND_ENABLED=true
```

Unset / `native` → workers use their own LLM exactly as before (zero change).

Built-in agents and how they're invoked (`KNOWN_AGENTS`):

| Backend | Command | Prompt delivery |
|---|---|---|
| `claude-code` | `claude -p` | stdin |
| `codex` | `codex exec <prompt>` | arg |
| `gemini-cli` | `gemini -p <prompt>` | arg |
| `aider` | `aider --yes --message <prompt>` | arg |

## Adding a new agent (no code change)

Any headless CLI agent works via an override:

```bash
SOVEREIGN_AGENT_BACKEND=my-agent
SOVEREIGN_BACKEND_CMD='["my-agent", "run", "--json"]'   # JSON list or a shell string
SOVEREIGN_BACKEND_PROMPT_VIA=arg                        # arg | stdin
SOVEREIGN_AGENT_BACKEND_TIMEOUT=1200
SOVEREIGN_AGENT_BACKEND_ENABLED=true
```

The backend feeds the task (system prompt + description + any code context + an
instruction to make the changes and get the tests passing) to the agent, runs it in
the task's `workspace_root`, and reads the result. If the agent supports
`--output-format json`, the last JSON line's `result`/`text`/`output` field is used;
otherwise raw stdout is the deliverable.

## How this adapts to platform updates

- **Stable seam, changing tools.** The `AgentBackend` interface (`execute_task`) does
  not change when a CLI adds flags or a new agent ships — you update a template or add
  a `KNOWN_AGENTS` entry. The engine, auction, audit, and ledger are untouched.
- **MCP as the universal connector.** Claude Agent SDK and Codex both speak MCP.
  Sovereign-OS ships an MCP *client* (`sovereign_os/mcp/`) that discovers a server's
  tools at connect time (`MCPToolGraph.get_tools_for_skill`), so tools a platform adds
  flow to workers without code changes. Point it at new MCP servers to gain new
  connectors.
- **Config over forks.** Command templates, prompt delivery, timeouts, per-skill
  routing, and the enable gate are all env-driven — a deployment tracks platform churn
  by editing config, not the codebase.

## Safety

Spawning an external agent runs code and tools on the host — the same risk class as
`SOVEREIGN_CODE_EXEC_ENABLED`. It is therefore **dry-run by default**: selection
(`SOVEREIGN_AGENT_BACKEND`) chooses a backend, but nothing spawns until
`SOVEREIGN_AGENT_BACKEND_ENABLED=true`, and a backend error always falls back to the
native LLM path rather than failing the task. Run external agents in a sandbox/CI
environment you control.
