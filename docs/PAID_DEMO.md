# 真实接单赚钱 Demo：清晰步骤

目标：用 Sovereign-OS **真实接单、执行任务、通过 Stripe 收费**，并可选把结果回传给客户。按下面步骤做完即可跑通一笔「订单 → 执行 → 扣款 → 交付」的闭环。

---

## Demo 如何体现 CEO / CFO / 动态权限

跑一笔付费 Job 时，系统**已经**在背后做这三件事；Dashboard 的 **Decision stream（决策流）** 和 **Trust** 会体现出来。

| 角色 | 在做什么 | 你在哪能看到 |
|------|----------|--------------|
| **CEO（Strategist）** | 把客户 goal 拆成任务计划：几个 task、各自技能（如 summarize / research）、预估 token。 | 决策流里 **「CEO: Plan created — N tasks. Goal: …」**；下方 Tasks 卡片里每个 task 的 skill。 |
| **CFO（Treasury）** | 每笔 task 执行前做**预算审批**：余额是否够、是否超过当日 burn 上限；高金额还可触发合规二次审批。 | 决策流里 **「CFO: Approved N task(s), est. $X. Balance: $Y.»**；若余额不足会整单失败并打日志 **CFO denied budget**。 |
| **动态权限（SovereignAuth）** | 每个 task 派发前检查该 **Agent 的 TrustScore** 是否达到该能力门槛（如 SPEND_USD 需 80 分）。审计通过 → 加分；失败 → 扣分，下次可能被拒。 | 顶栏 **Trust** 显示当前执行过的 agent 的 TrustScore；决策流里 **「CFO dispatch: Task X → agent (permission OK)」** 表示权限通过。若某 agent 因审计多次失败导致分数不足，会出现 **permission_denied**。 |

**如何更直观感受「动态分配权限」**：  
- 顶栏的 **Trust** 即当前 agent 的 TrustScore（默认 50；审计通过 +5，失败 -15）。  
- 能力门槛：READ_FILES 10、SPEND_USD 80 等（见代码 `sovereign_os/agents/auth.py`）。  
- 若希望看到「权限被拒」：可把某 agent 的 TrustScore 调低（或故意让任务产出空/劣质触发审计失败），再跑一单，观察该 agent 是否被拒。

---

## 一、前期准备（约 10 分钟）

### 1.1 Stripe 账号

- 打开 [Stripe 官网](https://stripe.com) 注册/登录。
- **测试阶段**：在 [Developers → API keys](https://dashboard.stripe.com/test/apikeys) 拿到 **Test mode** 的 `sk_test_...`。
- **正式收费**：切换到 Live mode，使用 `sk_live_...`（上线前务必确认环境与合规）。

### 1.2 LLM API Key

- 二选一即可：**OpenAI**（`OPENAI_API_KEY`）或 **Anthropic**（`ANTHROPIC_API_KEY`）。
- 用于 CEO 规划、审计和内置 Worker（摘要、研究、写稿等）。

### 1.3 安装并配置 Sovereign-OS

```bash
cd Sovereign-OS
pip install -e ".[llm,payments]"
cp .env.example .env
```

编辑 `.env`，**至少**填写：

```env
STRIPE_API_KEY=sk_test_xxxx
ANTHROPIC_API_KEY=sk-ant-xxxx
# 或 OPENAI_API_KEY=sk-xxxx
```

可选但**强烈建议**（持久化与自动接单）：

```env
SOVEREIGN_JOB_DB=./data/jobs.db
SOVEREIGN_LEDGER_PATH=./data/ledger.jsonl
SOVEREIGN_AUTO_APPROVE_JOBS=true
SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true
```

说明：

- `SOVEREIGN_AUTO_APPROVE_JOBS=true`：新单自动批准，无需在 Dashboard 点 Approve，适合「真实接单」demo。
- `SOVEREIGN_COMPLIANCE_AUTO_PROCEED=true`：若配置了支出阈值，超过也自动放行，不卡二次人工。

---

## 二、定价与订单内容

- 每笔 Job 的金额由 **`amount_cents`** 决定（单位：美分），例如：
  - 摘要类：500 美分 = 5 美元  
  - 研究/写稿类：1000 美分 = 10 美元  
- 创建 Job 时在 body 里带 `goal`（任务描述）和 `amount_cents` 即可，例如：
  - `"goal": "Summarize the key points of quantum computing in 3 paragraphs."`
  - `"amount_cents": 500`

---

## 三、订单从哪里来（选一种方式即可）

### 方式 A：用 curl / Postman 模拟「客户下单」（最快验证）

本机启动服务后，直接调 API 创建一笔付费单：

```bash
# 启动服务（先在一个终端运行）
python -m sovereign_os.web.app
```

另开终端：

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Summarize the benefits of remote work in one paragraph.",
    "amount_cents": 500,
    "currency": "USD"
  }'
```

若配置了 `SOVEREIGN_API_KEY`，需加请求头（二选一）：

- `X-API-Key: 你的密钥`
- 或 `Authorization: Bearer 你的密钥`

返回里会有 `job_id`、`status` 等。在 Dashboard（http://localhost:8000）的 Job queue 里可看到该单；若已开自动批准，会直接变为 approved 并开始执行。

### 方式 B：用「Ingest 轮询」接单（适合订单来源是 JSON 的脚本/后端）

1. 准备一个**可被 HTTP 访问的 JSON 地址**，返回格式例如：

```json
[
  { "goal": "Summarize X in one paragraph.", "amount_cents": 500, "currency": "USD" },
  { "goal": "Research pros and cons of Y.", "amount_cents": 1000, "currency": "USD" }
]
```

或 `{ "jobs": [ ... ] }`（见 [CONFIG.md](CONFIG.md)）。

2. 在 `.env` 里设置：

```env
SOVEREIGN_INGEST_URL=https://你的域名或内网地址/orders.json
SOVEREIGN_INGEST_INTERVAL_SEC=60
SOVEREIGN_INGEST_DEDUP_SEC=300
```

3. 重启 Web 服务。系统会按间隔轮询该 URL，把新 job 入队；配合 `SOVEREIGN_AUTO_APPROVE_JOBS=true` 即自动接单、执行、收费。

### 方式 C：做一个简单「下单页」POST 到你的 API（真实客户可填表单）

1. 做一个静态页或小后端，表单包含：任务描述（对应 `goal`）、金额（转成 `amount_cents`）、可选 `callback_url`（用于交付结果）。
2. 表单提交时，用 JavaScript 或后端请求：

   `POST https://你的域名/api/jobs`  
   Body: `{ "goal": "...", "amount_cents": 500, "currency": "USD", "callback_url": "可选" }`

3. 若设置了 `SOVEREIGN_API_KEY`，需要在你的前端或后端里带上 API Key（不要在前端写死密钥，建议用你自己的后端转发并加 Key）。也可用 `SOVEREIGN_JOB_IP_WHITELIST` 限制只允许你的服务器 IP 调用。

---

## 四、配置「交付 / 通知客户」（Webhook）

任务完成或支付失败时，Sovereign-OS 会向一个 URL 发 POST（方便你通知客户或写库）。

1. 准备一个**可公网访问的 URL**（你的后端接口），例如：  
   `https://你的域名/webhook/sovereign-job-done`

2. 在 `.env` 里设置：

```env
SOVEREIGN_WEBHOOK_URL=https://你的域名/webhook/sovereign-job-done
SOVEREIGN_WEBHOOK_SECRET=随便设一串密钥
```

3. 你的后端收到 POST 后：
   - 用 `SOVEREIGN_WEBHOOK_SECRET` 校验请求头里的 `X-Sovereign-Signature`（HMAC-SHA256），确认是 Sovereign 发的。
   - 解析 JSON：含 `job_id`、`status`、`goal`、`result_summary`、`payment_id` 等，据此发邮件/短信/Slack 给客户，或写入订单状态。

单笔订单也可在创建 Job 时传 `callback_url`，则该笔完成后会优先 POST 到该 URL（格式同全局 Webhook）。详见 [CONFIG.md](CONFIG.md)。

---

## 五、跑通一笔「接单 → 执行 → 收费 → 交付」

1. **启动**  
   ```bash
   python -m sovereign_os.web.app
   ```
   打开 http://localhost:8000 看 Dashboard。

2. **发一单**（用方式 A 的 curl，或你在方式 B/C 里准备的入口）  
   - 例如：`goal` = "Summarize the benefits of open source in 3 bullet points."，`amount_cents` = 500。

3. **观察**  
   - Dashboard 的 Job queue：该单先 pending，若开了自动批准会变成 approved → running → completed。  
   - Balance / Ledger：完成后应看到收入（例如 +$5.00）。  
   - Stripe Dashboard（Test mode）：[Payments](https://dashboard.stripe.com/test/payments) 里应有一条对应扣款。

4. **交付**  
   - 若配置了 `SOVEREIGN_WEBHOOK_URL`，你的后端会收到完成回调，用其中的 `result_summary` 等字段通知客户或更新订单状态。

---

## 六、检查清单（避免「看起来没赚钱」）

| 项 | 说明 |
|----|------|
| Stripe 密钥 | `.env` 里是 `sk_test_...`（测试）或 `sk_live_...`（正式），且服务已重启。 |
| LLM Key | 至少配置了 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`，否则任务可能走 Stub 无真实输出。 |
| 自动批准 | `SOVEREIGN_AUTO_APPROVE_JOBS=true` 时，新单会自动执行；否则需在 Dashboard 点 Approve。 |
| 金额 | `amount_cents > 0` 才会调 Stripe 扣款；为 0 则只跑任务不收费。 |
| Webhook | 若收不到回调，查 `SOVEREIGN_WEBHOOK_LOG_PATH`（失败会写日志）、网络与 URL 是否可从运行 Sovereign 的机器访问。 |

---

## 七、小结：最小可跑通流程

1. 配置 `.env`：`STRIPE_API_KEY`、一个 LLM Key、`SOVEREIGN_AUTO_APPROVE_JOBS=true`（及可选 Ledger/Job DB 路径）。  
2. 启动：`python -m sovereign_os.web.app`。  
3. 用 curl 或你自己的页面发一单：`POST /api/jobs`，带 `goal` 和 `amount_cents`。  
4. 在 Dashboard 与 Stripe 里确认：Job 完成、有扣款、Ledger 有收入。  
5. 需要「通知客户」时：设 `SOVEREIGN_WEBHOOK_URL`（及可选 `SOVEREIGN_WEBHOOK_SECRET`），在你的后端处理 POST 并发邮件/Slack 等。

更多环境变量与安全建议见 [CONFIG.md](CONFIG.md)，收费与合规逻辑见 [MONETIZATION.md](MONETIZATION.md)。
