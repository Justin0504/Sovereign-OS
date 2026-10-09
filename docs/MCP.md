# Sovereign-OS over MCP

Two directions, and they need opposite treatments.

## Serving: governed execution inside the client you already use

Adopting a governance layer normally means adopting a runtime — clone it, write a
charter, run a console. That is a large ask for something whose value only appears once
it sits in the path of real work. Over MCP it is one config entry, and the agent you are
already using starts clearing a budget check, carrying scoped authority, and leaving a
verifiable receipt.

```jsonc
// ~/.claude/claude_desktop_config.json  (or .mcp.json for Claude Code)
{
  "mcpServers": {
    "sovereign-os": {
      "command": "python",
      "args": ["-m", "sovereign_os.mcp.server"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-…",
        "SOVEREIGN_DATA_DIR": "~/.sovereign-os"
      }
    }
  }
}
```

```bash
pip install 'sovereign-os[mcp]'
```

### What it exposes

| Tool | Answers |
|---|---|
| `forecast_cost` | what will this cost, before I run it |
| `run_governed` | do the work under a budget and a scoped grant |
| `audit_receipt` | what happened, and can I verify it myself |
| `governance_status` | what limits are in force, and how good are the estimates |

`SOVEREIGN_DATA_DIR` is where the ledger, trust store and learned cost calibration live.
It is reused across calls on purpose: a fresh engine per call would reset the history
every time and turn governance into theatre.

### What it deliberately does not expose

There is no tool to raise a budget, grant a capability, or disable a check. **A
governance layer whose own controls are reachable from the agent it governs is
decorative** — the first thing a model does on hitting a ceiling is look for the lever.
The levers are in the charter and the environment, outside the protocol surface. A test
pins this, because it is the kind of thing a later convenience PR quietly undoes.

## Consuming: MCP tools under governance

Sovereign-OS also connects out to MCP servers for tools. That path had no authority check
at all: a registered tool could be invoked by any task, regardless of what that task was
approved to do. An MCP tool reaches the filesystem, the network, or somebody's API, so
"a tool is registered" was standing in for "this task may use it".

Every call now passes the delegation gate. `CALL_EXTERNAL_API` is the floor, since an MCP
call leaves the process by definition. Servers whose tools do more declare it:

```bash
export SOVEREIGN_MCP_CAPABILITIES='{"filesystem": ["write_files"], "shell": ["execute_shell"]}'
```

Declared per server rather than inferred from a tool's name — a name-based classifier
standing between an agent and the filesystem is a guess wearing a policy's clothes. A
malformed declaration falls back to the floor rather than widening authority.

A refusal is returned to the model as an observation, not raised:

```
(refused: grant grant-1 does not authorize call_external_api for tool web:fetch_url)
```

The model can then choose another route, where an exception would abort work that may
still be completable.

### Strict mode

`SOVEREIGN_STRICT_DELEGATION=1` turns an ungoverned call — no broker, or no grant on the
task — into a refusal. The default is permissive so the single-tenant self-host keeps
working, which is a fail-open default and therefore not a posture to serve other people
from. The hosted deployment sets it.
