# 架构 FAQ：Agent 从哪来、订单如何对接、CEO 如何管权限

## 1. 我操控的 Agent 员工从哪来？

Agent（Worker）的来源有三类，由 **WorkerRegistry** 统一管理：

| 来源 | 说明 | 如何配置 |
|------|------|----------|
| **默认兜底** | 未注册的技能都用 `StubWorker`（占位，返回固定输出供审计）。 | `registry.set_default(StubWorker)`（引擎默认已设） |
| **显式注册** | 你把「技能名 → 具体 Worker 类」注册进去，例如 `research` → `SummarizerWorker`。 | `registry.register("research", SummarizerWorker)`，在创建 `GovernanceEngine` 前对传入的 `registry` 调用 |
| **MCP 自招（Phase 5）** | 没注册时，若配置了 `MCPToolGraph`，会根据技能从 MCP 工具图里找对应工具，自动用 **MCPWorker** 执行。 | 创建 `GovernanceEngine(..., mcp_tool_graph=graph)`，并让 MCP 服务里暴露与 `skill_tool_map` 匹配的工具 |

**谁决定「用哪个技能」？**  
**CEO（Strategist）** 根据目标 + Charter 的 `core_competencies` 生成 `TaskPlan`，每个任务带 `required_skill`（如 `research`、`code`）。  
Registry 按 `required_skill` 解析出「用哪个 Worker」；你通过 **Charter 的能力列表 + Registry 的注册 / MCP 图** 间接「操控」有哪些员工、谁来做哪类事。

---

## 2. 外部订单如何交付、如何对接？

### 订单如何进来（对接入口）

| 方式 | 说明 |
|------|------|
| **HTTP API** | 外部系统 `POST /api/jobs`，body：`{"goal": "...", "amount_cents": 100, "currency": "USD", "charter": "可选"}`。可选设 `SOVEREIGN_API_KEY`，请求时带 `X-API-Key`。 |
| **轮询拉取** | 设 `SOVEREIGN_INGEST_URL` 为你的接口地址，返回 JSON 数组或 `{"jobs": [...]}`，每项含 `goal`、可选 `charter`、`amount_cents`、`currency`。系统按 `SOVEREIGN_INGEST_INTERVAL_SEC` 轮询并自动入队。 |

详见 [CONFIG.md](CONFIG.md) 的 24/7 & ingestion 小节。

### 订单如何「交付」（当前与可扩展）

- **当前**：任务跑完后，结果在系统内部（`TaskResult`、日志、Dashboard 的 Activity）。收费通过 Stripe 完成，收入记入 Ledger；**不会自动把「任务结果」推回给外部系统**。
- **若要对接到你的业务**：  
  - **方案 A**：你的系统主动轮询 `GET /api/jobs`（或后续可加的 `GET /api/jobs/{id}`），根据 `status` 和（若我们暴露）`result` 取结果。  
  - **方案 B**：在代码里加「Job 完成回调」：在 Web 里 job 状态变为 `completed` 时，向你在 Charter 或 env 里配置的 URL 发一条 POST（例如 `job_id`、`goal`、`status`、`output` 摘要），由你的服务接收并做交付。

---

## 3. 大脑 CEO 如何动态管理 Agent 权限？

**CEO（Strategist）** 只负责「规划」：把目标拆成任务、给每个任务打上 `required_skill`，**不直接管权限**。  
**权限** 由 **SovereignAuth** 根据 **TrustScore** 动态决定，引擎在**派发前**和**审计后**会调用它。

### 流程简述

1. **派发前**：引擎要执行某任务时，先算 `agent_id`（例如竞价赢家或 `{skill}-{task_id}`），再按任务技能映射到所需 **Capability**（如 `research` → READ_FILES，`code` → WRITE_FILES，`spend` → SPEND_USD）。然后调用 **`SovereignAuth.check_permission(agent_id, capability)`**：只在该 agent 的 **TrustScore ≥ 该能力所需阈值** 时返回 True，否则本次不执行并记失败。
2. **审计后**：任务被 Auditor 校验后，引擎调用 **`record_audit_success(agent_id)`** 或 **`record_audit_failure(agent_id)`**，TrustScore 加减分（可配置），从而**下次**该 agent 是否还能拿到高权限能力，由新分数决定。
3. **竞价**：若启用 BiddingEngine，Treasury 选标时会按 TrustScore 打折（分数低则出价竞争力下降），形成「表现差 → 分数低 → 难接单 / 难拿高权限」的闭环。

### 能力与阈值（默认）

| Capability | 默认最低 TrustScore |
|------------|----------------------|
| READ_FILES | 10 |
| CALL_EXTERNAL_API | 50 |
| WRITE_FILES | 40 |
| EXECUTE_SHELL | 60 |
| SPEND_USD | 80 |

即：新 agent 默认 50 分，只能做读文件、部分写文件等；要能「花美金」需要先通过多次审计把分数提到 80+。你可在创建 `SovereignAuth` 时传入 `capability_thresholds` 和 `base_trust_score` 自定义。

---

## 小结

- **Agent 从哪来**：Registry 的默认 Worker、你注册的 Worker、以及（可选）MCP 工具图自动生成的 MCPWorker；CEO 只决定「要什么技能」，不决定具体实现。  
- **外部订单对接**：通过 `POST /api/jobs` 或 `SOVEREIGN_INGEST_URL` 入队；交付目前需你自己轮询或我们加「完成回调」把结果推到你系统。  
- **CEO 与权限**：CEO 只规划任务和技能；权限由 SovereignAuth 按 TrustScore 动态控制，引擎在派发前检查、审计后更新分数，从而实现「大脑」对 agent 权限的间接、动态管理。
