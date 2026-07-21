# AI 工艺选型配置系统（ai_quotation）

> **主目标：** 根据工艺五要素及其他信息，自动生成 **详细配置清单（BOM）** 与 **设备配置参数**，经人工审核确认后落库。  
> **工程重点：** **Agent Harness**（守卫 + 审计）+ **双 Loop**（Clarify 补全 / 待审扫描）+ **选型经验闭环**（人审反馈 → 归纳 → 下次自动套用）。  
> **大模型：** 经上游 AI 数据中台 MCP `chat_completion` 调用（密钥与型号在中台配置，本应用不持有 `DASHSCOPE_API_KEY`）。

目录名 `ai_quotation` 保留兼容；产品语义是 **工艺选型配置**，不是自动商务报价。

本目录可整体拷贝为独立仓库，不依赖中台源码，仅依赖已启动的中台 **MCP / API** 与 **PostgreSQL**。

---

## 目录

1. [项目亮点](#1-项目亮点)
2. [业务背景与目标](#2-业务背景与目标)
3. [技术栈](#3-技术栈)
4. [系统架构](#4-系统架构)
5. [Harness 与双 Loop](#5-harness-与双-loop)
6. [选型经验闭环](#6-选型经验闭环)
7. [流水线与五要素](#7-流水线与五要素)
8. [模块说明](#8-模块说明)
9. [存储设计](#9-存储设计)
10. [与 AI 数据中台的边界](#10-与-ai-数据中台的边界)
11. [环境配置](#11-环境配置)
12. [快速开始](#12-快速开始)
13. [演示路径](#13-演示路径)
14. [常见问题](#14-常见问题)
15. [简历描述参考](#15-简历描述参考)

---

## 1. 项目亮点

| 优先级 | 亮点 | 说明 |
|--------|------|------|
| ★★★ | **配置清单 + 设备参数** | 主交付：BOM 式配套清单与主机/系统关键参数表 |
| ★★★ | **选型经验闭环** | 人审自然语言修订 → 结构化经验 → 下次生成自动套用 |
| ★★★ | **Agent Harness** | `wrap_run` 审计、追问轮数硬顶、事件落 `harness_events` |
| ★★★ | **双 Loop** | Clarify Loop 问全五要素；HarnessLoop 扫待审配置积压 |
| ★★ | **人机协同** | LangGraph `interrupt`：确认 / 自然语言修订 / 拒绝 |
| ★★ | **规则可演示** | 无 LLM 也能出完整清单；有 MCP 可润色/抽槽/归纳经验 |
| ★★ | **MCP 解耦** | 检索 + 大模型统一走中台；Token 按项目计量 |
| ★ | **领域术语** | d95/D95 = 通筛率，不是细度；细度用目数/D50 |

---

## 2. 业务背景与目标

粉体设备选型高度依赖人工经验：客户用自然语言描述物料、细度、产量、进料尺寸等，工程师再手工拼配置清单。易漏项、口径不一，经验难以复用。

本系统目标：

1. 用自然语言采集 **工艺五要素**（缺则 Clarify Loop 追问）；
2. 规则引擎生成 **配置清单 + 设备参数** 初稿；
3. 人工审核可用自然语言指出不合理处（如「入料已达磨机要求，不用加破碎机」）；
4. 系统把意见沉淀为经验，后续同类条件自动改清单；
5. 与知识中台解耦：检索/大模型走 MCP，业务表落独立 PostgreSQL Schema。

---

## 3. 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| UI | Streamlit | 需求对话 / 配置审核 / 历史 / 选型经验 |
| 编排 | LangGraph + Postgres Checkpoint | 有状态流程、interrupt 人审恢复 |
| 守卫 | 自研 QuotationHarness / HarnessLoop | 审计、轮数硬顶、待审扫描 |
| 生成 | `config_engine` 规则引擎 | 无 LLM 也可出清单 |
| 协议 | MCP Streamable HTTP | `search_documents` / `chat_completion` |
| 存储 | PostgreSQL | `ai_inquiry_quotation` schema |
| 配置 | python-dotenv | 本目录 `.env` + 可选继承中台根 `.env` |

---

## 4. 系统架构

```text
┌──────────────────────────────────────────────────────────────┐
│  Streamlit（:8504）                                           │
│  需求对话 │ 配置审核 │ 历史方案 │ 选型经验                      │
└────────────────────────────┬─────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  QuotationService + QuotationHarness.wrap_run                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ LangGraph                                               │  │
│  │ parse → Clarify↺ → generate_config(+经验召回)            │  │
│  │        → human_review → finalize(+经验学习) → END        │  │
│  └────────────────────────────────────────────────────────┘  │
│  HarnessLoop（可选）：扫描 pending_human                       │
└───────────────┬─────────────────────────────┬────────────────┘
                │ MCP                         │ SQL
                ▼                             ▼
     ┌──────────────────┐          ┌──────────────────────┐
     │ AI 数据中台 MCP   │          │ PostgreSQL           │
     │ search / chat    │          │ inquiries / configs  │
     │ completion       │          │ experiences / events │
     └──────────────────┘          │ + LangGraph CKPT     │
                                   └──────────────────────┘
```

---

## 5. Harness 与双 Loop

```text
┌────────────────────────────────────────────┐
│  HarnessLoop：定时扫描 pending_human       │
├────────────────────────────────────────────┤
│  QuotationHarness：wrap_run / 轮数守卫     │
├────────────────────────────────────────────┤
│  Clarify Loop：补全五要素                   │
│  → generate_config(+经验) → 人审 → 学习落库 │
└────────────────────────────────────────────┘
```

| 组件 | 作用 |
|------|------|
| **Clarify Loop** | 缺五要素 → interrupt 追问 → 补充 → 再解析 |
| **QuotationHarness** | 所有 start/resume 经 `wrap_run`；事件含 `config_generated` / `experience_applied` / `experience_learned` / `configured` |
| **HarnessLoop** | `HARNESS_LOOP_INTERVAL_SEC>0` 时后台扫待审 |

配置：`MAX_CLARIFY_LOOPS`、`HARNESS_LOOP_INTERVAL_SEC`。

---

## 6. 选型经验闭环

对应场景：人工发现方案不合理（例如入料 6mm 已达磨机要求，清单仍有破碎机）→ 审核页写修订意见 → 归纳为规则 → 后续同类条件自动改清单。

```text
人审修订意见
    ↓ learn_from_feedback（MCP LLM，失败则启发式）
config_experiences（条件 + 动作）
    ↓ 下次 generate_config 后 retrieve_and_apply
改写 line_items / warnings / 参数
```

### 动作类型

| `action_type` | 含义 | 示例 |
|---------------|------|------|
| `remove_line` | 去掉匹配清单行 | 目标 `破碎\|预碎\|颚破` |
| `add_warning` | 写入方案警告 | d95 是通筛率不是细度 |
| `set_param` | 改写设备参数键值 | 功率 / 风量等 |
| `note` | 仅记经验，生成时提示 | 暂无强动作的意见 |

### 召回打分（可解释，非向量）

- 约 70%：条件文本关键词 Jaccard  
- 约 25%：`slot_hints` 与当前槽位命中  
- 约 20% 加分：`remove_line` 且当前清单已出现目标设备  

≥ `EXPERIENCE_APPLY_THRESHOLD`（默认 **0.35**）则套用。  
硬约束：「去掉破碎」类经验仅在 **入料 ≤15mm** 时生效。

---

## 7. 流水线与五要素

```text
自然语言
  → parse_slots（规则 + 可选 MCP LLM）
  → 缺五要素？→ Clarify Loop …
  → generate_config（规则清单 + 经验召回 + 可选 MCP/概述润色）
  → human_review interrupt
  → finalize（落库 + 经验学习）→ END
```

| 必填字段 | 含义 |
|----------|------|
| `material` | 加工物料 |
| `fineness` | 成品细度（目数 / D50；**不是 d95**） |
| `sieve_pass_rate` | 通筛率（**含 d95/D95**） |
| `capacity` | 产量 |
| `feed_size` | 进料尺寸 |

规则侧：进料 **>5mm** 时默认带「预破碎机」（偏保守），可被人审经验纠正。

---

## 8. 模块说明

| 文件 | 说明 |
|------|------|
| `app.py` | Streamlit：需求 / 审核 / 历史 / 选型经验 |
| `service.py` | 对外门面 + 注入 Harness / 经验 / Graph |
| `harness.py` | wrap_run、轮数守卫、HarnessLoop |
| `config_engine.py` | 配置清单 + 设备参数规则生成 |
| `experience.py` | 经验学习 / 召回 / 套用 |
| `mcp_llm.py` | 封装中台 `chat_completion` |
| `mcp_client.py` | MCP HTTP/TCP 客户端 |
| `slots.py` | 五要素抽槽（启发式 + 可选 LLM） |
| `schemas.py` / `store.py` / `db.py` | 模型、落库、连接 |
| `graph/` | LangGraph 状态机 |
| `start.py` | 启动入口（默认 :8504） |

---

## 9. 存储设计

Schema 默认 `ai_inquiry_quotation`（`APP_DB_SCHEMA`）：

| 表 | 内容 |
|----|------|
| `inquiries` | 需求单、槽位、draft、状态 |
| `configurations` | 人审确认后的正式清单与参数 |
| `config_experiences` | 选型经验（条件、动作、命中次数） |
| `messages` | 对话与系统消息 |
| `harness_events` | 审计事件 |
| LangGraph checkpoint | interrupt 恢复 |

---

## 10. 与 AI 数据中台的边界

| 中台负责 | 本应用负责 |
|----------|------------|
| 知识库检索 `search_documents` | 工艺对话、Clarify、配置生成 |
| 大模型 `chat_completion`、型号、Token 计量 | 人审、经验库、业务 Schema |
| 项目鉴权 / MCP 网关 | Streamlit 选型工作台 |

**凭证：**

- `MCP_CLIENT_TOKEN`：必须与中台 MCP 网关密钥一致（**不要**用项目 JWT 冒充）  
- `SERVICE_TOKEN` / `API_BEARER_TOKEN`：仅用于直连中台 REST（可选）  
- 大模型密钥只配在中台 `DASHSCOPE_API_KEY`

---

## 11. 环境配置

```bash
cd models/ai_quotation
cp .env.example .env
```

| 变量 | 含义 | 默认 |
|------|------|------|
| `PROJECT_ID` | 中台项目 UUID（检索与 Token 归属） | — |
| `MCP_CLIENT_TOKEN` | MCP 网关 Bearer | — |
| `MCP_URL` | MCP 地址 | `http://127.0.0.1:8765/mcp` |
| `LLM_MODEL` | 可选覆盖型号；**留空用中台项目配置** | 空 |
| `MAX_CLARIFY_LOOPS` | Clarify 最大轮数 | `5` |
| `HARNESS_LOOP_INTERVAL_SEC` | 待审扫描间隔；`0`=关 | `0` |
| `EXPERIENCE_APPLY_THRESHOLD` | 经验召回阈值 | `0.35` |
| `APP_DB_SCHEMA` | 业务表 schema | `ai_inquiry_quotation` |
| `PORT` | Streamlit 端口 | `8504` |

修改代码后若 UI 行为未变，请重启进程（`st.cache_resource` 会缓存 Service）。

---

## 12. 快速开始

**前置：** 中台 API + MCP 已启动；PostgreSQL 可用；中台已配置 `DASHSCOPE_API_KEY`（若要用 LLM）。

```bash
cd models/ai_quotation
cp .env.example .env   # 填写 PROJECT_ID、MCP_CLIENT_TOKEN、DB
uv sync                # 或 pip install -r requirements.txt
uv run python start.py # http://localhost:8504
```

---

## 13. 演示路径

1. **Clarify：** 故意漏一项五要素 → 看追问 → 补全。  
2. **配置初稿：** 进料写 `6mm` → 「配置审核」可见预破碎机（规则偏保守）。  
3. **经验学习：** 修订意见写「入料已达磨机要求，不用加破碎机」→ 确认 → 「选型经验」出现规则；本单破碎项被去掉。  
4. **经验召回：** 再开一单进料 `6mm` → 生成后自动去掉预破碎；`40mm` 单仍保留。  
5. **中台用量：** 在中台「大模型管理 → Token 用量」可见本项目消耗。  

---

## 14. 常见问题

| 问题 | 处理 |
|------|------|
| MCP 401 | 检查 `MCP_CLIENT_TOKEN` 是否与中台网关一致，勿填项目 SERVICE_TOKEN |
| 有项目配置但型号仍不对 | 确认本应用 `.env` 未强制写死 `LLM_MODEL` |
| 无 LLM 也能跑吗 | 可以：规则抽槽 + 规则出清单；经验归纳可走启发式 |
| 提示词管理改了为何无效 | 选型用任务 prompt，不读中台「提示词管理」（该页主要服务客服） |

---

## 15. 简历描述参考

**项目名称：** AI 工艺选型配置系统（Harness + 双 Loop + 经验闭环）

**描述：**  
面向粉体设备工艺选型，基于 LangGraph 采集工艺五要素，经 Clarify Loop 强制补全后，由规则引擎生成详细配置清单与设备参数；人工可用自然语言指出不合理处，系统归纳为选型经验并在后续单据自动召回套用。自研 Agent Harness 做入口审计与追问轮数守卫；检索与大模型经 MCP 对接中台，Token 按项目计量。将 Agent 从「能对话」提升为「可出配置、可纠偏学习、可控可观测」的线上能力。

---

## License

与所属仓库 / 组织约定一致。
