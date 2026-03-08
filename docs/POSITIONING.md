# Sovereign-OS 定位：接单来源与你的服务

本文档说明：**单从哪来**、**我们提供什么**、**你如何用自定义 Worker 交付给客户**。

---

## 接单来源（Order sources）

订单可以来自：

| 来源 | 说明 |
|------|------|
| **开源 / 社区平台上的用户诉求** | 在 Reddit、论坛、Discord、GitHub 等地方，有人发帖求写稿、求调研、求总结等。通过 Ingest Bridge 或你的爬虫/机器人把这些诉求转成 Job，推给 Sovereign-OS。 |
| **合作方** | 合作方（乙方、渠道、集成商）把他们的客户需求通过 API（`POST /api/jobs`）或约定的 ingest URL 推给你。你提供「执行 + 审计 + 收款」能力，他们负责获客与交付界面。 |
| **知道这个 API 的人 / 推文与传播** | 任何人只要知道你的 API 或 Dashboard，就可以发单：自己的脚本、推文里附的链接、文档里的示例。你提供的是「可接单的操作系统」，谁带来流量谁就带来订单。 |

你不需要「造」需求，只需要**对接这些来源**（ingest、API、合作方系统），剩下的由 Sovereign-OS 完成：规划、控费、执行、审计、收款、交付。

---

## 我们提供什么

- **一套操作系统（Sovereign-OS）**  
  宪章（Charter）→ CEO 规划 → CFO 控费 → 执行 → 审计 → Ledger 记账。你接到的每一单都在这套治理里跑完。

- **基础 Worker**  
  16 个内置 Worker：summarize、research、reply、write_article、write_email、translate、spec_writer、code_assistant 等。开箱即用，配置 Stripe + 一个 LLM key 就能接单、收款、交付。

- **扩展与个性化由你完成**  
  你可以**创建自己的 Worker**，挂到同一套 OS 上，用同一套 Charter / CFO / 审计 / Ledger。这样你提供的是**你定义的服务**（你的领域、你的 prompt、你的交付格式），我们提供的是**运行这些服务的操作系统**。

---

## 你的服务 = 你的 Worker + 你的接单渠道

- **接单**：来自开源平台、合作方、或任何知道并调用你 API 的渠道。  
- **执行**：Sovereign-OS + 基础 Worker + **你写的自定义 Worker**。  
- **交付**：Webhook / callback / Reddit 回复 / 邮件等，由你和 Charter 决定。

因此：**我们提供 Operating System 和基础 Worker；你通过自定义 Worker 个性化你提供的服务，并把你自己的接单渠道带来的需求，交付给客户。**

---

## 如何创建自己的 Worker

1. 阅读 [WORKER.md](WORKER.md)：实现 `BaseWorker`，注册到 `WorkerRegistry`，并在 Charter 里声明对应 `core_competencies`。
2. Web 部署下可把自定义 Worker 放在 `sovereign_os/agents/user_workers/`，或通过 API/配置注册；详见 [WORKER.md](WORKER.md) 与 [CONFIG.md](CONFIG.md)。
3. 用你的 Worker 处理你关心的任务类型，用内置 Worker 处理通用任务；CEO 会按 Charter 把目标拆成任务并分发给对应 Worker。

---

## 一句话总结

**接单来源**：开源平台上的用户诉求、合作方、任何知道并调用你 API 的推文/脚本/合作。  
**我们提供**：Operating System + 基础 Worker。  
**你来做**：创建个性化 Worker，对接你的接单渠道，把服务交付给你的客户。
