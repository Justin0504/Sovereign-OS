# MCP Tool Graph & Self-Hiring (Phase 5)

Sovereign-OS can **discover tools from MCP (Model Context Protocol) servers** and use them to fulfill tasks when no Worker is registered for a skill. This is "self-hiring": the system dynamically assigns work to MCP-backed agents.

## Concepts

- **MCPToolGraph**: Caches tools from one or more MCP servers. You register servers with `add_server(server_id, client)` after connecting; the graph then maps **skills** (e.g. `research`, `code`) to available tools using `sovereign_os.mcp.tool_mapping.skill_tool_map`.
- **MCPWorker**: A Worker that runs a task by calling a single MCP tool (the first matching tool for the skill). It uses the task description as the main argument (e.g. `query`, `path`, or `input`).
- **Registry integration**: When you pass `mcp_tool_graph` into `WorkerRegistry` (or into `GovernanceEngine`), `get_bidders(skill)` will include an `mcp-{skill}` bidder when the graph has tools for that skill. `get_worker(..., agent_id="mcp-research")` then returns an `MCPWorker` configured with those tools.

## Skill → tool mapping

Edit `sovereign_os/mcp/tool_mapping.py` to define which MCP tool names belong to which skills:

```python
skill_tool_map: dict[str, list[str]] = {
    "research": ["read_file", "search", "fetch_url"],
    "code": ["read_file", "write_file", "run_terminal_cmd", "list_dir"],
    "audit": ["read_file", "search"],
    # ...
}
```

When a task requires `research`, the graph looks for any of these tool names in registered servers and returns the first match per tool name.

## Wiring the graph

1. **Create and populate the graph** (e.g. at startup):

```python
from sovereign_os.mcp import MCPClient, MCPToolGraph

graph = MCPToolGraph()
client = MCPClient(transport="stdio", command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
await client.connect()
await graph.add_server("fs", client)
# graph.discover_skills() -> skills that have at least one tool available
# graph.get_tools_for_skill("research") -> [(server_id, MCPToolSchema), ...]
```

2. **Pass the graph into GovernanceEngine**:

```python
from sovereign_os import load_charter, UnifiedLedger
from sovereign_os.agents import SovereignAuth
from sovereign_os.auditor import ReviewEngine
from sovereign_os.governance import GovernanceEngine
from sovereign_os.mcp import MCPToolGraph

charter = load_charter("charter.example.yaml")
ledger = UnifiedLedger()
# ... build graph and add_server ...

engine = GovernanceEngine(
    charter,
    ledger,
    auth=SovereignAuth(),
    review_engine=ReviewEngine(charter),
    mcp_tool_graph=graph,
)
plan, results, reports = await engine.run_mission_with_audit("Summarize the market.")
```

3. **Bidding**: If you use `BiddingEngine`, the CFO can choose between a regular worker (e.g. StubWorker) and `mcp-{skill}` when both are returned by `get_bidders`. The MCP worker will be instantiated with the graph’s tools and client when selected.

## MCPWorker behavior

- Uses the **first** tool in the list returned by `get_tools_for_skill(skill)`.
- Builds tool arguments from `TaskInput`: common keys (`query`, `path`, `command`, etc.) are mapped in `mcp_worker._TOOL_ARG_KEY`; otherwise the task description is sent as `query` or `input`.
- Returns `TaskResult` with `output` from the MCP tool response (or an error message on failure).

## Adding more Charters and adapters

- **Charters**: Add YAML files under `charters/` and reference them when loading. Ensure `core_competencies` and `success_kpis` match the skills you use (including those provided by MCP).
- **Adapters**: To ingest jobs from new sources (RSS, email), implement a poller or webhook that calls the same enqueue API (e.g. `POST /api/jobs` or the internal job store). The existing `SOVEREIGN_INGEST_URL` poller is one adapter; you can add more in `sovereign_os/ingest/`.
