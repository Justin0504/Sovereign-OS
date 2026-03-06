# 后续开发方案（Future Development Plan）

在「接单 → 批准 → 执行 → 审计 → 收费 → Webhook」闭环和 24/7、**16 内置 Worker** 已就绪的前提下，本文档给出**分波次、可执行的**后续开发方案，便于按优先级排期或拆 Issue。

**Wave 3 & 4 已完成**：assistant_chat / code_assistant / code_review Worker、Job 并发（`SOVEREIGN_JOB_WORKER_CONCURRENCY`）、`POST /api/jobs/batch`、多实例文档（[MULTI_INSTANCE.md](MULTI_INSTANCE.md)）、Good First Issues 文档、Release 流程、恢复测试（`test_job_store_persists_across_restart`）、IP 白名单（`SOVEREIGN_JOB_IP_WHITELIST`）。

---

## 当前状态简要

| 类别 | 已完成 |
|------|--------|
| **核心闭环** | 接单（API + Ingest）、人工/自动批准、执行与审计、Stripe 收费、Ledger 记账、完成 Webhook |
| **开源就绪** | 默认 Charter、16 内置 Worker、QUICKSTART、config_warnings、入参校验、限流、重试、Webhook 失败落盘、batch API、Job 并发、IP 白名单 |
| **可观测** | /health（含队列与模式提示）、Token 使用、审计轨迹、可选 Prometheus |

详见 [OPEN_SOURCE_READY_PLAN.md](OPEN_SOURCE_READY_PLAN.md) 与 [OPTIMIZATION_ROADMAP.md](OPTIMIZATION_ROADMAP.md)。

---

## Wave 1：稳定性与可观测（建议优先）

**目标**：生产环境更稳、问题更好排查。投入小，收益高。

| 序号 | 方向 | 具体动作 | 参考 |
|------|------|----------|------|
| 1.1 | **优雅退出** | 收到 SIGTERM 时，等当前正在跑的 Job 完成再退出，避免执行到一半被 kill | OPTIMIZATION_ROADMAP § 一 |
| 1.2 | **/health 增强** | 暴露 `jobs_running`、`jobs_pending` 细分；可选「最近一次 Job 完成时间」或「队列积压数」 | § 二 |
| 1.3 | **request_id / 链路** | 为每个 Job 或 API 请求生成 `request_id`，写入日志与 Webhook payload，便于从 Dashboard/Webhook 反查日志 | § 二 |
| 1.4 | **生产环境检查** | 启动时若检测到 `sk_live_` 或未设 `SOVEREIGN_API_KEY`，在 /health 或日志中给出明显警告 | § 三 |
| 1.5 | **限流与边界单测** | 为 `POST /api/jobs` 限流、`amount_cents` 边界、400/429 响应码补单测，防止回归 | § 七 |

**交付标准**：部署后可安全重启；运维能从 /health 和日志快速判断队列与配置状态；关键路径有单测覆盖。

---

## Wave 2：体验与部署友好

**目标**：新用户和运维更容易上手、更容易 24/7 部署。

| 序号 | 方向 | 具体动作 | 参考 |
|------|------|----------|------|
| 2.1 | **一键部署示例** | 提供 `docker-compose` 或单机脚本示例：`.env.example` 说明、推荐 volume 挂载、可选 Redis/Web 组合 | § 四 |
| 2.2 | **示例 Ingest 端点** | 提供静态 JSON 或最小 mock 服务示例，方便本地测试 `SOVEREIGN_INGEST_URL` | § 四 |
| 2.3 | **README 首屏截图/GIF** | 用实际运行截图或短视频替代占位，突出 24/7、接单、Dashboard、健康检查 | § 四 |
| 2.4 | **队列与 Ledger 备份** | 文档或脚本：定期备份 `SOVEREIGN_JOB_DB` 和 `SOVEREIGN_LEDGER_PATH`，便于灾难恢复 | § 一 |

**交付标准**：新用户能按文档一键起环境；有可复用的 Ingest 示例；首屏视觉上能传达「可接单、可观测」。

---

## Wave 3：能力与规模

**目标**：更多 Worker、更高吞吐、可选水平扩展。

| 序号 | 方向 | 具体动作 | 参考 |
|------|------|----------|------|
| 3.1 | **assistant_chat Worker** | 通用问答/对话技能：goal 无明显「写/翻译/纪要」等时，由 Strategist 派发到 `assistant_chat`，更贴近 Claude 式对话 | § 六 |
| 3.2 | **code_assistant / code_review Worker** | 代码理解、修改建议、简单 Code Review 输出（仅 LLM 分析，不执行代码） | § 六 |
| 3.3 | **Job 并发数** | 配置项如 `SOVEREIGN_JOB_WORKER_CONCURRENCY=2`，允许多个 Job 并行执行，提高吞吐 | § 一 |
| 3.4 | **批量创建 Job API** | `POST /api/jobs/batch` 一次提交多条 goal，减少调用次数 | § 六 |
| 3.5 | **多实例队列（可选）** | 若需多实例水平扩展，引入 Redis 等作为队列后端，共享 Job 状态 | § 五 |

**交付标准**：支持「说不清具体技能」的对话类 goal；支持代码相关任务；高负载下可调并发；有批量入队能力；可选多实例部署方案。

---

## Wave 4：社区与长期

**目标**：便于外部贡献、版本可追踪、传播清晰。

| 序号 | 方向 | 具体动作 | 参考 |
|------|------|----------|------|
| 4.1 | **Good First Issue** | 在 GitHub 用 `good first issue` 等标签标出文档、示例、单测类 issue，方便贡献者上手 | § 八 |
| 4.2 | **发版与 Changelog** | 按版本维护 CHANGELOG，重要改动打 tag，Release 说明可引用 CHANGELOG 段落 | § 八 |
| 4.3 | **恢复测试（可选）** | 进程中途 kill 后重启，验证持久化队列与 Ledger 恢复正确，可选加入 CI | § 七 |
| 4.4 | **IP 白名单（可选）** | 对 `POST /api/jobs` 或 Ingest 来源做可选 IP 白名单，适合内网/固定出口 | § 三 |

**交付标准**：新贡献者有明确入口；用户能按版本号跟进；关键持久化路径有回归保障；企业内网场景有可选锁域手段。

---

## 建议实施顺序（一句话）

1. **Wave 1**：先做优雅退出、/health 增强、request_id、生产环境检查、限流/边界单测。  
2. **Wave 2**：再做一键部署示例、Ingest 示例、README 截图、备份文档。  
3. **Wave 3**：按需做 assistant_chat、code_review Worker、并发与批量 API、多实例队列。  
4. **Wave 4**：持续做 Good First Issue、发版/Changelog、可选恢复测试与 IP 白名单。

如需更细的任务拆解或与现有 Issue 对齐，可直接从 [OPTIMIZATION_ROADMAP.md](OPTIMIZATION_ROADMAP.md) 中按「优先级」列筛选高/中优先项，拆成独立 Issue 或 PR。
