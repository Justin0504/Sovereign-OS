# Quick Start：上手即用

只需配置 **Stripe** 和 **一个 LLM API Key**（OpenAI 或 Anthropic），即可接单、执行基础任务并收费。

---

## 三步开始

### 1. 克隆并安装

```bash
git clone https://github.com/YourUsername/Sovereign-OS.git
cd Sovereign-OS
pip install -e ".[llm]"
```

如需 Stripe 收费演示，可选安装支付依赖：

```bash
pip install -e ".[payments]"
```

### 2. 配置环境变量

复制示例配置并填写**必填项**：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

| 变量 | 必填 | 说明 |
|------|------|------|
| `STRIPE_API_KEY` | 是（若需收费） | Stripe 密钥，测试用 `sk_test_...` |
| `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` | 二选一 | 用于 CEO 规划、审计与内置 Worker（摘要/研究/回复） |

**仅用 Anthropic 测试时**：只设置 `ANTHROPIC_API_KEY` 即可，无需设置 `SOVEREIGN_LLM_PROVIDER`，系统会自动使用 Anthropic。若需指定模型，可设置：

```bash
SOVEREIGN_LLM_PROVIDER=anthropic
SOVEREIGN_LLM_MODEL=claude-3-5-sonnet-20241022
```

可选（推荐持久化）：

- `SOVEREIGN_JOB_DB=./data/jobs.db` — 任务队列持久化
- `SOVEREIGN_LEDGER_PATH=./data/ledger.jsonl` — 账本持久化
- `SOVEREIGN_AUDIT_TRAIL_PATH=./data/audit.jsonl` — 审计轨迹持久化

**Human-out-of-loop（无人值守接单）**：

- `SOVEREIGN_AUTO_APPROVE_JOBS=true` — 新 Job 自动批准，无需在 Dashboard 点 Approve；配合 `SOVEREIGN_INGEST_URL` 可实现全自动接单→执行→收费→Webhook。
- `SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true` — 当设置了支出阈值时，超过阈值也自动放行，不要求二次人工批准。详见 [CONFIG.md](CONFIG.md)。

### 3. 启动 Web 控制台

```bash
python -m sovereign_os.web.app
```

或使用项目提供的脚本（会从 `.env` 加载变量）：

- Windows: `run_paid_demo.bat`
- 或: `docker compose up web`

打开浏览器访问 **http://localhost:8000**。

---

## 首次发单与收费

1. 在 Dashboard 的 **Mission** 输入框输入目标，例如：`Summarize the market in one paragraph.`
2. 点击 **Run**，系统会使用默认 Charter（`charter.default.yaml`）和内置 Worker 规划并执行。
3. 若使用 **Job queue** 收费流程：
   - 用 `POST /api/jobs` 或示例脚本提交带金额的 Job（见 [examples/README.md](../examples/README.md)）。
   - 在 Dashboard **Job queue** 中批准该 Job，执行完成后会自动扣款并记入 Ledger。

---

## 内置通用技能（Built-in skills）

开箱即用（默认引擎已注册）：

- `summarize`：摘要
- `research`：简短调研（要点 + 结论）
- `reply`：模板回复（支持 `{{var}}`）
- `write_article`：写文章（标题选项/大纲/草稿/要点）
- `solve_problem`：解题/解问题（理解→步骤→最终答案）
- `write_email`：写邮件（主题 x3 + 正文）
- `write_post`：写社媒 post（多版本 + CTA）
- `meeting_minutes`：会议纪要（决策/行动项/风险）
- `translate`：翻译（保留格式）
- `rewrite_polish`：改写润色（不新增事实）
- `collect_info`：收集信息（研究计划 + 临时结论 + 验证清单）
- `extract_structured`：结构化抽取（JSON + 缺失字段）
- `spec_writer`：写规格/SOW（范围/交付物/验收/风险/问题）

## 健康检查与配置提示

访问 **http://localhost:8000/health** 可查看：

- `status`: 服务状态
- `ledger`: 账本是否可用
- `config_warnings`: 若未设置 Stripe 或 LLM Key，会在此列出，便于排查「静默回退到 Dummy」问题。

---

## 接单方式

- **方式一**：在 Web UI 的 Mission 输入目标并 Run（即时执行，无队列）。
- **方式二**：`POST /api/jobs` 提交 Job（body: `goal`, `amount_cents`, `currency`, 可选 `charter`, `callback_url`），在 Dashboard 批准后执行并收费。
- **方式三**：设置 `SOVEREIGN_INGEST_URL`，系统会轮询该 JSON 地址并将新 Job 入队。详见 [CONFIG.md](CONFIG.md) 与 [MONETIZATION.md](MONETIZATION.md)。

### 示例：用 curl 提交付费 Job

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"goal":"Summarize the AI market in one paragraph.","amount_cents":100,"currency":"USD"}'
```

若设置了 `SOVEREIGN_API_KEY`，需加请求头：

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_SOVEREIGN_API_KEY" \
  -d '{"goal":"Research AI trends.","amount_cents":200,"currency":"USD","callback_url":"https://your-server.com/webhook"}'
```

---

## 常见问题

| 现象 | 处理 |
|------|------|
| 启动后扣款仍显示 Dummy | 检查 `.env` 中 `STRIPE_API_KEY` 是否填写；重启 Web 进程；查看 `/health` 的 `config_warnings`。 |
| 任务一直 Stub 无真实摘要 | 设置 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`，并安装 `pip install -e ".[llm]"`。 |
| 想只用 Anthropic | 只配置 `ANTHROPIC_API_KEY` 即可，无需设置 `SOVEREIGN_LLM_PROVIDER`。 |
| Webhook 收不到 | 确认 `SOVEREIGN_WEBHOOK_URL` 或 Job 的 `callback_url` 可公网访问；查看应用日志中的 webhook 重试记录。 |

---

## 下一步

- 内置技能与 Charter：见 [CHARTER.md](CHARTER.md)、[WORKER.md](WORKER.md)。
- 支付与人工审批：见 [MONETIZATION.md](MONETIZATION.md)。
- 完整配置项：见 [CONFIG.md](CONFIG.md)。
