# Architecture FAQ: Where agents come from, how orders are delivered, how the CEO manages permissions

## 1. Where do the agents (workers) I control come from?

Agents (workers) come from three sources, all managed by **WorkerRegistry**:

| Source | Description | How to configure |
|--------|--------------|-------------------|
| **Default fallback** | Unregistered skills use `StubWorker` (placeholder, fixed output for audit). | `registry.set_default(StubWorker)` (engine default) |
| **Explicit registration** | You register "skill name → Worker class", e.g. `research` → `SummarizerWorker`. | `registry.register("research", SummarizerWorker)` before creating `GovernanceEngine` |
| **MCP self-hire (Phase 5)** | If no registration and `MCPToolGraph` is set, the system finds tools by skill and runs **MCPWorker**. | Create `GovernanceEngine(..., mcp_tool_graph=graph)` and expose tools matching `skill_tool_map` in MCP |

**Who decides which skill is used?**  
The **CEO (Strategist)** produces a `TaskPlan` from the goal and Charter `core_competencies`; each task has a `required_skill` (e.g. `research`, `code`).  
The registry resolves `required_skill` to a Worker; you indirectly "control" who does what via the **Charter competency list + registry registration / MCP graph**.

---

## 2. How are external orders delivered and integrated?

### How orders enter (integration points)

| Method | Description |
|--------|-------------|
| **HTTP API** | External system `POST /api/jobs` with body: `{"goal": "...", "amount_cents": 100, "currency": "USD", "charter": "optional"}`. Optional `SOVEREIGN_API_KEY`; send `X-API-Key` in requests. |
| **Polling** | Set `SOVEREIGN_INGEST_URL` to your endpoint returning a JSON array or `{"jobs": [...]}` with `goal`, optional `charter`, `amount_cents`, `currency`. System polls at `SOVEREIGN_INGEST_INTERVAL_SEC` and enqueues. |

See [CONFIG.md](CONFIG.md) section 24/7 & ingestion.

### How orders are "delivered" (current and extensible)

- **Current:** After a task runs, the result stays inside the system (`TaskResult`, logs, Dashboard Activity). Payment is via Stripe and recorded in the Ledger; **task results are not pushed back to external systems by default**.
- **To integrate with your system:**  
  - **Option A:** Your system polls `GET /api/jobs` (or a future `GET /api/jobs/{id}`) and uses `status` and (if exposed) `result`.  
  - **Option B:** Use "job completion callback": when a job becomes `completed`, the Web layer POSTs to a URL you configure (Charter or env) with e.g. `job_id`, `goal`, `status`, `output` summary; your service receives it and delivers to the client.

---

## 3. How does the CEO dynamically manage agent permissions?

The **CEO (Strategist)** only **plans**: it breaks the goal into tasks and assigns `required_skill` to each; it **does not manage permissions directly**.  
**Permissions** are decided by **SovereignAuth** from **TrustScore**; the engine calls it **before dispatch** and **after audit**.

### Flow

1. **Before dispatch:** When the engine is about to run a task, it computes `agent_id` (e.g. auction winner or `{skill}-{task_id}`), maps the task skill to a **Capability** (e.g. `research` → READ_FILES, `code` → WRITE_FILES, `spend` → SPEND_USD). It then calls **`SovereignAuth.check_permission(agent_id, capability)`**: only if that agent’s **TrustScore ≥ threshold** for that capability does it return True; otherwise the task is not run and recorded as failed.
2. **After audit:** After the Auditor checks the task, the engine calls **`record_audit_success(agent_id)`** or **`record_audit_failure(agent_id)`**; TrustScore is updated (configurable), so **next time** that agent’s ability to get high-permission work depends on the new score.
3. **Bidding:** If BiddingEngine is enabled, the Treasury factors TrustScore into the bid (lower score → less competitive bid), so "bad performance → low score → fewer jobs / less permission" forms a loop.

### Capabilities and default thresholds

| Capability | Default min TrustScore |
|------------|-------------------------|
| READ_FILES | 10 |
| CALL_EXTERNAL_API | 50 |
| WRITE_FILES | 40 |
| EXECUTE_SHELL | 60 |
| SPEND_USD | 80 |

So: a new agent starts at 50 and can only do read and some write; to "spend USD" it must pass enough audits to reach 80+. You can override via `capability_thresholds` and `base_trust_score` when creating `SovereignAuth`.

---

## Summary

- **Where agents come from:** Registry default worker, your registered workers, and (optionally) MCP workers from the MCP tool graph; the CEO only decides "which skill", not the implementation.  
- **External order integration:** In via `POST /api/jobs` or `SOVEREIGN_INGEST_URL`; delivery today is either your own polling or a "completion callback" that pushes results to your system.  
- **CEO and permissions:** The CEO only plans tasks and skills; permissions are controlled dynamically by SovereignAuth via TrustScore; the engine checks before dispatch and updates scores after audit, so the "brain" indirectly and dynamically manages agent permissions.
