# 工艺选型配置系统（ai_quotation）

> **一句话**：根据工艺「五要素」等信息，自动生成设备配置方案（配置清单 / 参数），并可给人审核、沉淀经验。  
> 智能客服在对话里问选型时，会 **HTTP 调用** 本系统的推荐接口；**缺什么参数由本系统判定**，客服负责向用户追问。

目录名 `ai_quotation` 是历史名字；产品语义是 **工艺选型配置**，不是自动算出商务报价单。  
报价意向（姓名、电话）由 **智能客服** 收集，不走本系统人审页。

---

## 目录

1. [它解决什么问题](#1-它解决什么问题)
2. [和智能客服怎么分工](#2-和智能客服怎么分工)
3. [什么是「五要素」](#3-什么是五要素)
4. [两种用法：工作台 vs 客服 API](#4-两种用法工作台-vs-客服-api)
5. [第一次跑起来](#5-第一次跑起来)
6. [推荐 API 怎么用（给联调者）](#6-推荐-api-怎么用给联调者)
7. [系统内部大概怎么跑](#7-系统内部大概怎么跑)
8. [环境变量](#8-环境变量)
9. [目录结构](#9-目录结构)
10. [常见问题](#10-常见问题)
11. [术语小词典](#11-术语小词典)

---

## 1. 它解决什么问题

工业磨粉/粉体设备选型时，销售或工艺人员需要根据：

- 加工什么物料  
- 要多细（细度）  
- 通筛率要求  
- 产量  
- 进料尺寸  
- …以及含水、硬度等补充信息  

才能给出较靠谱的设备配置。

本系统目标：

1. **自动生成**详细配置说明（规则引擎为主，可结合知识库/大模型）  
2. **工程师工作台**里可多轮补参、人工审核、改判  
3. **把人审结论沉淀为经验**，下次相似需求可自动参考  
4. **给智能客服一条同步 HTTP 捷径**：对话中快速要方案，不走进人审打断流程  

大模型调用走 **中台 MCP**（本应用不持有 `DASHSCOPE_API_KEY`）。

---

## 2. 和智能客服怎么分工

请牢记这一句：

> **客服问人，本系统判齐并出方案；要留资找销售由客服负责。**

| 系统 | 负责 | 不负责 |
|------|------|--------|
| **本系统** | 五要素校验；生成配置方案；工程师人审与经验学习；`/api/v1/recommend` | 客服聊天 UI；CRM 留资字段；公司报价审批流 |
| **智能客服** | 多轮对话、把缺失项问出来、展示方案、询问是否报价并写线索 | 另搞一套五要素规则；在客服里做人审 BOM |

时序（客服调用时）：

```text
用户在客服里描述需求
  → 客服工具 process_config_recommend
  → POST 本系统 /api/v1/recommend
       ├─ 缺五要素 → 返回 missing + clarify_question
       │     → 客服向用户追问 → 再 POST（带上已收集 slots）
       └─ 齐全 → 返回 proposal_text + structured
             → 客服展示方案 →（可选）报价留资
```

---

## 3. 什么是「五要素」

推荐接口认为「必填」的核心槽位通常包括：

| 槽位（概念） | 含义 | 注意 |
|--------------|------|------|
| 加工物料 | 原料是什么 | 如石英砂、矿渣 |
| 成品细度 | 要磨到多细 | 目数 / D50 等；**不要和 d95 搞混** |
| 通筛率 | 过筛比例要求 | 常与 d95/D95 相关 |
| 产量 | 产能 | 单位要说清 |
| 进料尺寸 | 原料颗粒大小 | |

选填：含水、硬度、电源、倾向机型等。

术语提醒（客服提示词里也写了）：

- **d95 / D95** 多表示通筛相关，不要当成「细度」去追问  

---

## 4. 两种用法：工作台 vs 客服 API

| 入口 | 端口（默认） | 给谁 | 特点 |
|------|--------------|------|------|
| Streamlit 工作台 `start.py` | **8504** | 工艺/销售工程师 | 完整流程：补参、生成、**人审 interrupt**、经验学习 |
| FastAPI `api.py` | **8510** | 智能客服 | 同步推荐；**不进入**人审断点；缺参就返回 missing |

本地联调客服时，**至少要启动 `api.py`**。  
工作台是给人用的，不是客服调用链路的必经之路。

---

## 5. 第一次跑起来

### 5.1 依赖

1. PostgreSQL（本应用使用独立 schema，默认 `ai_inquiry_quotation`）  
2. 中台 API + MCP 已启动（若要用知识库检索 / LLM 增强）  
3. Python + `uv`

### 5.2 安装与配置

```bash
cd models/ai_quotation
cp .env.example .env
# 填写：PROJECT_ID、MCP_URL、MCP_CLIENT_TOKEN、数据库等
uv sync
```

### 5.3 启动

```bash
# 给客服用的推荐 API（联调必开）
uv run python api.py
# http://0.0.0.0:8510  健康检查：GET /health

# 工程师工作台（可选）
uv run python start.py
# http://localhost:8504
```

客服侧对应配置（在 `models/ai_customer/.env`）：

```text
PROCESS_CONFIG_URL=http://127.0.0.1:8510
# PROCESS_CONFIG_TOKEN=   # 若本系统开了 CUSTOMER_API_TOKEN，则两边填一样
```

---

## 6. 推荐 API 怎么用（给联调者）

**接口：** `POST /api/v1/recommend`

常见请求字段：

| 字段 | 说明 |
|------|------|
| `query` | 用户问题或客服整理的短查询 |
| `extras` | 追问过程中累积的补充说明 |
| `slots` | 已识别/已填写的槽位（多轮回传） |
| `session_id` | 可选，便于日志对齐 |

若配置了 `CUSTOMER_API_TOKEN`，请求头需要：

```text
Authorization: Bearer <token>
```

**缺参时（示意）：**

```json
{
  "ok": false,
  "error": "missing_required_slots",
  "missing": ["fineness", "capacity"],
  "clarify_question": "请补充成品细度与产量要求",
  "slots_partial": {}
}
```

**齐全时（示意）：**

```json
{
  "ok": true,
  "proposal_text": "……可读的配置说明……",
  "structured": {}
}
```

客服拿到 `ok=false` 只会追问 `missing`，不会在客服里「发明」另一套校验规则。

---

## 7. 系统内部大概怎么跑

### 7.1 工作台（完整图）

概念流程：

```text
收集/澄清五要素
  → 生成配置（规则引擎 + 可选检索/LLM）
  → 人工审核（可 interrupt 暂停等人）
  → 确认后落库
  → 人审反馈可写入「选型经验」，下次自动参考
```

外围有 **Harness**：限制循环次数、做审计，防止 Agent 乱跑。

### 7.2 客服捷径

`recommend_for_customer` 一类逻辑：  
只做「判齐 → 出方案」，**跳过人审 interrupt**，以保证 HTTP 同步返回。

### 7.3 经验闭环（工作台价值）

1. 工程师改判 / 确认某次配置  
2. 系统把可复用规则写入经验库  
3. 下次相似输入达到阈值可自动套用或给出建议  

这让系统越用越「像你们厂的真实口径」，而不是每次纯模型临场发挥。

---

## 8. 环境变量

详见 `.env.example`。新手优先关心：

| 变量 | 含义 |
|------|------|
| `PROJECT_ID` | 中台知识库项目（检索用） |
| `MCP_URL` / `MCP_CLIENT_TOKEN` | 连接中台 MCP |
| `APP_DB_SCHEMA` | 默认 `ai_inquiry_quotation` |
| `PORT` | 工作台端口，默认 8504 |
| `CUSTOMER_API_HOST` / `CUSTOMER_API_PORT` | 推荐 API 监听，默认 8510 |
| `CUSTOMER_API_TOKEN` | 推荐 API 鉴权（可选） |
| `MAX_CLARIFY_LOOPS` | 工作台追问上限 |
| `EXPERIENCE_APPLY_THRESHOLD` | 经验自动套用阈值 |

数据库可用 `DATABASE_URL` 或 `DB_*`（常继承仓库根 `.env`）。

---

## 9. 目录结构

```text
ai_quotation/
├── api.py              # 客服用的 FastAPI 推荐服务 :8510
├── start.py / app.py   # Streamlit 工作台
├── service.py          # 业务门面
├── config_engine.py    # 配置生成核心
├── slots.py            # 五要素槽位定义与处理
├── experience.py       # 经验匹配与学习
├── harness.py          # 护栏与审计
├── graph/              # LangGraph 状态机（工作台流程）
├── mcp_client.py / mcp_llm.py
├── store.py / db.py / schemas.py
└── .env.example
```

---

## 10. 常见问题

**Q：客服一直说工艺服务连不上？**  
A：是否执行了 `uv run python api.py`？端口是否为 8510？客服 `PROCESS_CONFIG_URL` 是否写对？

**Q：一直缺参数，追问很奇怪？**  
A：看 API 返回的 `missing` 字段；检查用户是否把 d95 和细度说混；可在工作台用同一描述试一次对比。

**Q：工作台能出方案，客服不行？**  
A：客服走的是 `api.py` 捷径，不是工作台端口 8504；不要把 UI 地址填进 `PROCESS_CONFIG_URL`。

**Q：需要大模型密钥吗？**  
A：配在中台根 `.env`；本目录通过 MCP 调用。规则引擎在无 LLM 时仍可给基础方案（视实现与配置而定）。

**Q：这是报价系统吗？**  
A：不是商务自动报价。这里产出的是 **工艺/设备配置方案**；商务价格与合同由人工/其它系统处理，客服只收集「想报价」的线索。

---

## 11. 术语小词典

| 词 | 白话解释 |
|----|----------|
| 五要素 / slots | 选型前必须齐的关键参数槽位 |
| BOM / 配置清单 | 方案里列出的设备与配置项（工程含义，不是购物车） |
| Clarify | 缺参时的追问补全 |
| 人审 interrupt | 流程暂停，等工程师在页面确认后再继续 |
| 经验库 | 历史人审沉淀下来的可复用规则/案例 |
| Harness | 限制循环、记录审计的护栏 |
| recommend API | 专供客服同步调用的推荐接口 |

相关文档：

- 数据中台：[../../README.md](../../README.md)  
- 智能客服：[../ai_customer/README.md](../ai_customer/README.md)  
- **客服 ↔ 本系统通信详解（代码 + 为何 HTTP）**：[../ai_customer/PROCESS_CONFIG_COMM.md](../ai_customer/PROCESS_CONFIG_COMM.md)  
