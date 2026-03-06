# 优化方向（Roadmap）

在现有「接单 → 自动批准 → 执行 → 审计 → 收费 → Webhook」闭环和 24/7 设计基础上，可按下面方向继续优化。按**优先级**和**投入**大致分为：快速可做、中期、长期。

**已完成（短期建议顺序）：** Webhook 失败落盘、POST /api/jobs 限流、Dashboard 模式提示、Job 入参校验、失败 Job 重试（`POST /api/jobs/{id}/retry` + `SOVEREIGN_JOB_MAX_RETRIES`）、E2E 含 Webhook mock 断言。见 [CONFIG.md](CONFIG.md) 中新增环境变量与 /health 字段。

---

## 一、可靠性 & 运维

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **失败 Job 重试** ✅ | 对 `failed` / `payment_failed` 的 Job 支持「重试一次」或可配置重试次数（仅限可重试错误），避免偶发网络/API 问题导致永久失败。`POST /api/jobs/{id}/retry`，`SOVEREIGN_JOB_MAX_RETRIES`。 | 高 |
| **优雅退出** | 进程收到 SIGTERM 时，等当前正在执行的 Job 跑完再退出，避免执行到一半被 kill。 | 中 |
| **Job 并发数** | 当前 worker 一次只跑一个 Job；可加配置（如 `SOVEREIGN_JOB_WORKER_CONCURRENCY=2`）允许多个 Job 并行执行，提高吞吐。 | 中 |
| **队列与 Ledger 备份** | 文档或脚本：定期备份 `SOVEREIGN_JOB_DB` 和 `SOVEREIGN_LEDGER_PATH`，便于灾难恢复。 | 低 |

---

## 二、可观测 & 排错

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **Webhook 失败落盘** ✅ | Webhook POST 失败时，将 payload 与错误写入 `data/webhook_log.jsonl`（或可配置路径），便于事后排查。`SOVEREIGN_WEBHOOK_LOG_PATH`。 | 高 |
| **POST /api/jobs 限流** ✅ | 按 IP 或 API Key 限制每分钟请求数（如 `SOVEREIGN_JOB_RATE_LIMIT_PER_MIN=60`），防止滥用。 | 高 |
| **/health 或 /metrics 增强** | 暴露更多指标：队列中 `pending`/`running` 数量、最近一次 Job 完成时间、可选 Prometheus 格式的 job 吞吐与延迟。 | 中 |
| **请求/Job 链路 ID** | 为每个 Job 或请求生成 `request_id`，在日志与 Webhook payload 中带上，便于从 Dashboard/Webhook 反查日志。 | 中 |

---

## 三、安全 & 合规

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **Job 入参校验** ✅ | 对 `POST /api/jobs` 的 `goal` 长度、`amount_cents` 上下界、`callback_url` 格式做校验，避免异常或恶意 payload。 | 高 |
| **生产环境检查** | 启动时若检测到生产配置（如 `sk_live_`、未设 `SOVEREIGN_API_KEY`）在 health 或日志中给出明显警告。 | 中 |
| **IP 白名单（可选）** | 对 `POST /api/jobs` 或 Ingest 来源做可选 IP 白名单，适合内网/固定出口场景。 | 低 |

---

## 四、体验 & 上手

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **Dashboard 模式提示** ✅ | 在 UI 上显示当前是否「自动批准」「合规自动放行」，避免用户误以为需要手动点 Approve。`/health` 返回 `auto_approve_jobs` / `compliance_auto_proceed`，Dashboard 展示。 | 高 |
| **一键部署示例** | 提供 `docker-compose` 或单机脚本示例，包含 `.env.example` 说明和推荐 volume 挂载，便于 24/7 部署。 | 中 |
| **示例 Ingest 端点** | 提供静态 JSON 或最小 mock 服务示例，方便用户本地测试 `SOVEREIGN_INGEST_URL`。 | 中 |
| **README 首屏 GIF/截图** | 用实际运行截图或短视频替代占位图，突出 24/7、接单、Dashboard 效果。 | 中 |

---

## 五、规模 & 性能

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **多实例队列** | 当前 SQLite 单写；若需多实例水平扩展，可引入 Redis 等作为队列后端，共享 Job 状态。 | 低 |
| **Charter/配置缓存** | 启动时加载 Charter 与配置后缓存，避免重复读文件（若已有则跳过）。 | 低 |

---

## 六、功能扩展

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **assistant_chat Worker** | 通用问答/对话技能：当 goal 无明显「写/翻译/纪要」等关键词时，由 Strategist 派发到 `assistant_chat`，更贴近「Claude 式」对话。 | 中 |
| **code_assistant / code_review Worker** | 代码理解、修改建议、简单 Code Review 输出，不执行代码，仅 LLM 分析。 | 中 |
| **批量创建 Job API** | `POST /api/jobs/batch` 一次提交多条 goal，减少调用次数。 | 低 |
| **Job 优先级或定时** | 队列支持优先级或「在指定时间后执行」，适合预约类需求。 | 低 |

---

## 七、测试 & 质量

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **E2E 含 Webhook mock** ✅ | 在现有 E2E 中增加：创建 Job → 自动批准 → 执行完成 → 断言 Webhook 被调用且 payload 含预期字段。见 `test_e2e_job_completion_fires_webhook` 与 `test_webhook_failure_writes_log`。 | 高 |
| **限流/边界单测** | 对 `POST /api/jobs` 限流逻辑、`amount_cents` 边界、错误响应码写单测。 | 中 |
| **恢复测试** | 进程中途 kill 后重启，验证持久化队列与 Ledger 恢复正确（可选 CI）。 | 低 |

---

## 八、社区 & 传播

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **Good First Issue 标签** | 在 GitHub 用 `good first issue` 等标签标出文档、示例、单测类 issue，方便贡献者上手。 | 中 |
| **中英双语 README 摘要** | README 顶部保留英文，增加简短中文「项目简介 + 快速开始」段落或链接。 | 低 |
| **发版与 Changelog** | 按版本维护 CHANGELOG，重要改动打 tag，便于用户跟进。 | 中 |

---

## 建议实施顺序（短期）

1. ~~**Webhook 失败落盘** + **POST /api/jobs 限流**~~ ✅ 已完成。
2. ~~**Dashboard 模式提示** + **Job 入参校验**~~ ✅ 已完成。
3. ~~**失败 Job 重试** + **E2E 含 Webhook mock**~~ ✅ 已完成。

下一步可做：优雅退出、/health 增强（pending/running 数量）、限流/边界单测、生产环境检查等（见上表）。
