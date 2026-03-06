# 工业级开源就绪计划：上手即用 + 接单 / 交付 / 主动联系

目标：用户**只需配置 Stripe + LLM API Key**，即可用自带 Worker 接单、执行基础任务、收费并交付（含主动通知客户）。本文档为执行计划，按阶段可拆分为 Issue/PR。

---

## 一、愿景与原则

| 原则 | 说明 |
|------|------|
| **上手即用** | 克隆 → 填 `.env`（Stripe + 至少一个 LLM Key）→ 启动 → 即可接单、跑任务、收费。 |
| **自带基础能力** | 内置若干 Worker，覆盖「摘要 / 简单研究 / 格式化回复」等基础任务，无需用户先写代码。 |
| **接单 + 交付闭环** | 支持 API/轮询接单；任务完成后支持**结果回调**与**主动通知**（Webhook），便于对接外部系统。 |
| **工业级** | 配置无密钥泄露、关键路径可观测、失败可重试、交付可追溯（审计轨迹、Ledger）。 |

---

## 二、当前能力清单（已有）

| 能力 | 状态 | 说明 |
|------|------|------|
| 接单入口 | ✅ | `POST /api/jobs`；`SOVEREIGN_INGEST_URL` 轮询入队 |
| 人工审批 | ✅ | Dashboard Job queue Approve；合规阈值可选 |
| 执行与审计 | ✅ | CEO 规划 → CFO 批预算 → Registry 派发 → Auditor 校验 → TrustScore 更新 |
| 收费与记账 | ✅ | Stripe 扣款（测试/生产）、Ledger 记收入 |
| 内置 Worker | ✅ | SummarizerWorker、ResearchWorker、ReplyWorker + 默认 Charter（summarize / research / reply） |
| 配置 | ✅ | `.env` + CONFIG.md；QUICKSTART 最少必配项；`/health` 返回 `config_warnings` |
| 主动联系/交付 | ✅ | Job 完成 Webhook（`SOVEREIGN_WEBHOOK_URL` + 单 Job `callback_url`），重试与 HMAC 签名 |

---

## 三、目标状态（就绪后）

- **用户动作**：复制 `.env.example` → 填 `STRIPE_API_KEY` + `OPENAI_API_KEY`（或 `ANTHROPIC_API_KEY`）→ 运行 `run_paid_demo.bat` 或 `docker compose up web`。
- **系统能力**：  
  - 用**内置默认 Charter + 内置 Worker** 接单并执行**摘要、简单研究、格式化回复**等基础任务。  
  - 收费走 Stripe；可选**完成 Webhook** 把结果推给用户后端；可选「通知客户」（如 Webhook 里含 `callback_url` 或由用户后端代为发邮件/短信）。

---

## 四、阶段规划

### Phase A：内置 Worker + 默认 Charter（「基础任务可交付」）

**目标**：不写代码即可跑通「接单 → 执行真实任务 → 审计 → 收费」。

| 序号 | 任务 | 产出 |
|------|------|------|
| A1 | **默认 Charter** | 仓库内 `charter.default.yaml`（或沿用 `charter.example.yaml` 并命名为默认），技能与内置 Worker 一一对应，如：`summarize`、`research`、`reply`。 |
| A2 | **内置 Worker 注册表** | 在引擎/Web 启动时，自动向 Registry 注册：`summarize` → SummarizerWorker（已有）；`research` → 新建 ResearchWorker（调用 LLM 做简短调研/提纲）；`reply` → 新建 ReplyWorker（按模板格式化回复，可带简单变量）。 |
| A3 | **Strategist 与技能对齐** | CEO 规划时只产出上述已注册技能（或回退到 Stub）；文档注明「默认支持的技能列表」。 |
| A4 | **依赖与文档** | `pip install -e .` 或 `pip install -e ".[llm]"` 后即可用；README 写明「最少配置：STRIPE_API_KEY + OPENAI_API_KEY」。 |

**交付标准**：新用户 clone → 填 2 个 key → 启动 → 发一单「Summarize …」→ Approve → 看到任务用 SummarizerWorker 完成并扣款。

---

### Phase B：最少配置 + 启动检查（「上手即用」）

**目标**：用户只关心 Stripe + 一个 LLM Key；启动时给出明确提示，避免「静默回退到 Dummy」。

| 序号 | 任务 | 产出 |
|------|------|------|
| B1 | **最少配置文档** | `docs/QUICKSTART.md`：三步（clone、填 `.env`、启动）；必填项：`STRIPE_API_KEY`、`OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`；可选：`SOVEREIGN_JOB_DB`、`SOVEREIGN_LEDGER_PATH`。 |
| B2 | **启动时配置检查** | 在 Web 启动时（或首次 `/health`）：若未设 Stripe Key 或任一 LLM Key，打 WARNING 或返回 200 + 字段 `config_warnings: ["STRIPE_API_KEY not set", "No LLM key set"]`，便于前端或运维发现。 |
| B3 | **.env.example 注释** | 在 `.env.example` 中标注必填/可选，并写「复制为 .env 后填写以下两项即可接单收费」。 |

**交付标准**：文档与注释一致；启动时若缺 key 有明确日志或 health 提示。

---

### Phase C：交付与主动联系（Webhook + 回调）

**目标**：任务/Job 完成后，系统能**主动**把结果推给用户或客户系统。

| 序号 | 任务 | 产出 |
|------|------|------|
| C1 | **Job 完成 Webhook（全局）** | 新增 env：`SOVEREIGN_WEBHOOK_URL`（可选）。当 Job 状态变为 `completed` 或 `payment_failed` 时，向该 URL 发一次 POST。**Payload 规范**见下文「Webhook 载荷」。请求头可带 `X-Sovereign-Signature`（HMAC-SHA256 of body，key 为 `SOVEREIGN_WEBHOOK_SECRET`）便于接收方校验。 |
| C2 | **Webhook 重试与日志** | 失败时重试 2～3 次（退避）；所有请求与响应状态记入日志；可选：写入 `data/webhook_log.jsonl` 便于排查。 |
| C3 | **单 Job 回调 URL（可选）** | 若 `POST /api/jobs` 的 body 支持 `callback_url`，则在该 Job 完成时优先 POST 到 `callback_url`，否则再使用全局 `SOVEREIGN_WEBHOOK_URL`。payload 与 C1 一致。 |
| C4 | **文档** | 在 CONFIG.md、MONETIZATION.md 中增加「交付与主动联系」小节：如何配置 Webhook、payload 格式、签名方式（若实现）、与「通知客户」的典型用法（用户后端收 Webhook 再发邮件/短信）。 |

**交付标准**：配置 `SOVEREIGN_WEBHOOK_URL` 后，Job 完成时用户后端能收到 POST；文档可让第三方据此实现「主动联系客户」。

---

### Phase D：接单与入队增强（工业级）

**目标**：接单方式清晰、可观测、可限流。

| 序号 | 任务 | 产出 |
|------|------|------|
| D1 | **接单方式文档** | 在 README 或 QUICKSTART 中单列「接单方式」：① 直接 `POST /api/jobs`；② 配置 `SOVEREIGN_INGEST_URL` 轮询；③ 可选 `SOVEREIGN_API_KEY` 保护。给出 curl 与 body 示例。 |
| D2 | **Ingest 去重** | 轮询到的 job 若与已有 job 的 `(goal, amount_cents, created_ts 窗口)` 重复，可跳过入队或标记为重复，避免重复执行（可选，按需实现）。 |
| D3 | **限流与可观测** | 对 `POST /api/jobs` 做简单限流（如按 IP 或 API Key 每分钟 N 条）；在 `/health` 或 `/metrics` 中暴露「队列长度」「今日入队数」等（若已有 Prometheus 则复用）。 |

**交付标准**：文档完整；高并发下不重复执行、可观测；按需上线限流。

---

### Phase E：测试、文档与发布

**目标**：新用户路径有测试保障，文档统一，版本可发布。

| 序号 | 任务 | 产出 |
|------|------|------|
| E1 | **端到端测试** | 一条 E2E：启动 app（或 test client）→ 创建 Job → Approve → 断言状态为 completed、Ledger 有 job_income、若启用 Webhook 则 mock 收到 POST。 |
| E2 | **QUICKSTART 与 README** | README 首屏「最少三步」指向 QUICKSTART；QUICKSTART 包含：Stripe + LLM Key、首次发单、查看 Stripe 与 Dashboard。 |
| E3 | **版本与 Release** | 上述阶段合并后打 tag（如 v0.3.0），CHANGELOG 列出：内置 Worker、默认 Charter、Webhook 交付、最少配置、文档改进。 |

---

## 五、内置 Worker 规格（Phase A 细化）

| 技能 ID | Worker | 输入 | 输出 | 依赖 |
|---------|--------|------|------|------|
| `summarize` | SummarizerWorker | task.description | 摘要段落 | LLM |
| `research` | ResearchWorker | task.description（主题/问题） | 要点 + 结论 | LLM |
| `reply` | ReplyWorker | 模板 + \|\| key=value | 填充后回复 | 模板 / LLM |
| `write_article` | ArticleWriterWorker | topic/audience/tone/length（context） | 标题选项/大纲/草稿/要点 | LLM |
| `solve_problem` | ProblemSolverWorker | task.description（题目） | 理解→步骤→答案 | LLM |
| `write_email` | EmailWriterWorker | to/purpose/tone（context） | 主题 x3 + 正文 | LLM |
| `write_post` | SocialPostWorker | platform/audience（context） | 多版本 post + CTA | LLM |
| `meeting_minutes` | MeetingMinutesWorker | task.description（记录） | 决策/行动项/风险/问题 | LLM |
| `translate` | TranslateWorker | target_language/style（context） | 译文（保留格式） | LLM |
| `rewrite_polish` | RewritePolishWorker | goal/tone（context） | 润色稿 + 修改说明 | LLM |
| `collect_info` | InfoCollectorWorker | depth/format（context） | 研究计划 + 临时结论 + 验证清单 | LLM |
| `extract_structured` | ExtractStructuredWorker | task.description + context.schema | JSON + 缺失字段说明 | LLM |
| `spec_writer` | SpecWriterWorker | task.description | SOW：范围/交付物/验收/风险/问题 | LLM |

默认 Charter（`charter.default.yaml`）的 `core_competencies` 列出上述全部技能，与 Registry 注册一致。详见 [WORKER.md](WORKER.md) 与 [QUICKSTART.md](QUICKSTART.md)。

---

## 六、主动联系与「通知客户」的形态

- **系统侧**：仅负责「在合适的时机、把结构化结果发给用户指定的端点」——即 **Job 完成 Webhook**（及可选的 per-job `callback_url`）。不内置发邮件/短信，避免依赖与隐私问题。
- **用户侧**：用户自己的服务接收 Webhook，再：
  - 存库、更新工单状态；
  - 调用邮件/短信/Slack API「主动联系客户」；
  - 或把 `result_summary` 展示在自有前台。

这样「主动联系」由用户业务实现，本系统只做可靠、可重试的**主动推送**。

---

## 六.1 Webhook 载荷规范（供实现与文档统一）

```json
{
  "job_id": "string",
  "status": "completed | payment_failed",
  "goal": "用户提交的目标摘要",
  "amount_cents": 100,
  "currency": "USD",
  "payment_id": "ch_xxx 或 null",
  "completed_at": "ISO8601",
  "result_summary": "任务输出摘要，建议 ≤2KB",
  "audit_score": 0.9,
  "charter": "Default"
}
```

- 接收方应幂等处理（同一 `job_id` 多次推送只处理一次）。
- 可选：`SOVEREIGN_WEBHOOK_SECRET` 时，请求头 `X-Sovereign-Signature: sha256=<hex>`，body 为 raw JSON 字符串的 HMAC。

---

## 七、配置总览（就绪后用户必填/可选）

| 配置项 | 必填 | 说明 |
|--------|------|------|
| `STRIPE_API_KEY` | 是（若需收费） | Stripe 密钥，测试用 `sk_test_`。 |
| `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` | 至少一个（若用内置 LLM Worker） | CEO/审计/Summarizer/Research 等依赖 LLM。 |
| `SOVEREIGN_JOB_DB` | 推荐 | 持久化队列，避免重启丢单。 |
| `SOVEREIGN_LEDGER_PATH` | 推荐 | 持久化账本。 |
| `SOVEREIGN_WEBHOOK_URL` | 可选 | Job 完成时主动 POST 交付结果。 |
| `SOVEREIGN_INGEST_URL` | 可选 | 轮询接单。 |
| `SOVEREIGN_API_KEY` | 可选 | 保护 `POST /api/jobs`。 |

---

## 八、实施顺序建议

1. **Phase A**：默认 Charter + 注册 Summarizer/Research/Reply，保证「基础任务可交付」。  
2. **Phase B**：QUICKSTART + 启动检查 + .env.example，保证「上手即用」与可发现配置问题。  
3. **Phase C**：Webhook + 单 Job callback_url（可选），保证「交付与主动联系」能力。  
4. **Phase D**：接单文档与去重/限流/可观测（按需）。  
5. **Phase E**：E2E 测试 + 文档定稿 + Release。

按此顺序，可先让「配置 Stripe + API Key 即能接单、交付基础任务、收费」闭环，再补齐「主动联系客户」的 Webhook 与文档，最终达到工业级、用户上手即用的开源状态。

---

## 八.1 实施进度（v0.3.0 已完成）

| 阶段 | 状态 | 备注 |
|------|------|------|
| Phase A | ✅ | charter.default.yaml；Summarizer / Research / Reply 内置并注册；Web 优先加载 default charter |
| Phase B | ✅ | QUICKSTART.md；`/health` 含 config_warnings；.env.example 注释 |
| Phase C | ✅ | SOVEREIGN_WEBHOOK_URL、callback_url、重试与 X-Sovereign-Signature；CONFIG/MONETIZATION 文档 |
| Phase D | ✅ | 接单方式在 QUICKSTART；`/health` 暴露 jobs_total、jobs_pending |
| Phase E | ✅ | E2E 与 webhook 单测；CHANGELOG v0.3.0；README 指向 QUICKSTART |

**后续可做（优化与增强）**：Phase D2 Ingest 去重已实现（可选 `SOVEREIGN_INGEST_DEDUP_SEC`）；Phase D3 限流与 `/metrics`；Webhook 失败落盘；更多 Worker。完整清单见 [OPTIMIZATION_ROADMAP.md](OPTIMIZATION_ROADMAP.md)。

---

## 九、安全与密钥（工业级要求）

| 项 | 要求 |
|----|------|
| 密钥不入库 | 仅通过环境变量或密钥管理服务读取，不在代码/README 中写真实 key。 |
| .env 不提交 | `.gitignore` 已含 `.env`；`.env.example` 仅占位符。 |
| Webhook 校验 | 接收方应校验 `X-Sovereign-Signature`（若配置了 `SOVEREIGN_WEBHOOK_SECRET`）。 |
| API 保护 | 生产环境建议设置 `SOVEREIGN_API_KEY`，避免未授权 `POST /api/jobs`。 |
| Stripe Webhook | 若接 Stripe 事件，必须校验 `Stripe-Signature`（已有 `STRIPE_WEBHOOK_SECRET`）。 |
