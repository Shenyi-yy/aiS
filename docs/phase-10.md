# Phase 10 — Zabbix Tool Calling（Agent 自主取证）

## 目标

把"人喂证据"（Phase 9）升级为"模型自己调工具取证再下结论"：

```
POST /api/v1/ai/agent/{problem_id}
  → 工单证据(同 P9) + 4 个只读 Zabbix 工具白名单
  → 模型回 tool_calls → 后端执行(走 zabbix_client 唯一出口) → 观察回填
  → 循环 ≤ LLM_AGENT_MAX_ROUNDS → 最终正文 + 全程 tool_trace 落库
```

后端版本 0.9.0 → **0.10.0**。`ai_analysis_records` 新增 `tool_trace` 列
（启动时幂等 ALTER，老库无损，Phase 9 记录照旧可查）。

## 设计决策

1. **只读白名单是结构性的**：`zabbix_tools.py` 注册表里只有 4 个查询工具
   （列 PROBLEM 事件 / 查事件详情 / 查主机监控项最新值 / 查当前 PROBLEM
   触发器）。写操作（item.create、trigger.delete、system.run…）在这里
   **根本不存在** —— "不让 AI 直接执行危险命令"不靠 prompt 自觉，靠代码
   结构；执行类能力要到 Phase 13 经人工确认才出现。
2. **工具异常 = 观察，不是崩溃**：Zabbix 报错/参数不合法一律消化成
   `{"error": ...}` JSON 回传给模型，让它换个姿势取证或承认缺证据。
3. **轮数护栏**：`LLM_AGENT_MAX_ROUNDS`（默认 6，范围 1-20）防模型无限
   调用工具烧 token；超限抛 LLMError 且**已发生的轨迹不丢** —— FAILED 行
   的 `tool_trace` 记录到第几轮、调了什么、失败原因，全可复盘。
4. **返回瘦身**：`lastvalue` 可能是 8KB 的 docker.info JSON，工具层统一
   截断（单值 200 字符、单次观察回填 4000 字符、limit 上限 50），防爆
   上下文也防爆延迟。
5. **协议层**：`llm_client.chat_raw()` 返回供应商原样 payload（含
   tool_calls），`chat()` 改成它的薄封装 —— Phase 9 行为不变（单测锁住）。
   assistant 的 tool_calls 消息与 tool 观察消息按 OpenAI 标准形状回填。
6. **7.0 铁律延续**：两个新语义方法都走 API 不碰 DB —— item 最新值只在
   `item.get` 返回里有（items 表无 lastvalue 列）；trigger 当前状态用
   `filter {"value": [1]}`（status 只是启用标志）。

## 新接口

```
GET  /api/v1/ai/tools               工具白名单回显（readonly 恒 true, 可核验）
POST /api/v1/ai/agent/{problem_id}  带取证的分析（多轮, 慢, 调用超时给 10 分钟）
```

## 文件地图

```
backend/app/services/llm_client.py      [改] +chat_raw(tools), chat 变薄封装
backend/app/services/zabbix_tools.py    [新] 只读工具定义+执行器+白名单红线
backend/app/services/ai_agent.py        [新] 多轮循环+轮数护栏+FAILED 带轨迹落库
backend/app/services/zabbix_client.py   [改] +fetch_host_items/+fetch_active_triggers
backend/app/db/models.py                [改] AIAnalysisRecord +tool_trace 列
backend/app/db/base.py                  [改] init_db 幂等补列(_ensure_columns)
backend/app/schemas/analysis.py         [改] +ToolInfo/ToolsList, AnalysisOut +tool_trace
backend/app/api/routes/analysis.py      [改] +GET /tools +POST /agent/{id}
backend/app/config.py                   [改] +llm_agent_max_rounds
backend/app/version.py / main.py        [改] 0.10.0
backend/tests/test_agent.py             [新] 7 个单测(循环/护栏/白名单/迁移)
docker-compose.yml / .env.example       [改] +LLM_AGENT_MAX_ROUNDS
scripts/verify_phase10.sh               [新] 6 项验证
```

## Ubuntu 操作要点

代码 scp 后重建即可，**不需要动 .env**（除非想调轮数）：

```bash
cd /opt/zops && docker compose -p zops up -d --build backend
```

启动日志应出现 `已补列: ai_analysis_records.tool_trace`（首次升级）；
Phase 9 的老记录不受影响。

## 常见错误及解决

| 现象 | 原因 | 处理 |
|---|---|---|
| [1/6] version≠0.10.0 | 没 `--build` 重建 | `docker compose -p zops up -d --build backend` |
| [3/6] analyses 查询 500 | 补列迁移没跑到 | `docker logs zops-backend --tail 50` 看启动异常；确认 db/base.py 是新版 |
| [4/6] 502 "最大工具轮数" | 模型反复取证不给结论（弱模型/工单复杂） | .env 加大 `LLM_AGENT_MAX_ROUNDS=10` 重建，或换更强的模型 |
| [5/6] no_tool_calls | 供应商网关不透传 tools 字段 / 模型不支持 function calling | `GET /api/v1/ai/config` 确认模型名；换支持工具调用的模型（DeepSeek 官方 API/Kimi/qwen 均可） |
| [4/6] 502 "LLM 返回 HTTP 400" 且提到 tools | 网关不认识 tools/tool_choice 参数 | 同上——这是供应商能力问题，不是代码问题 |
| Agent 特别慢 | 每轮都是一次完整大模型往返 × Zabbix 查询 | 正常，1-5 分钟；[4/6] curl 已给 600s 超时 |

## 与后续阶段的接线

- Phase 11：再挂一组 Docker/Linux **只读**工具（复用 zabbix_tools 的
  白名单+观察模式，执行器换成后端已有的 docker summary / agent 键通道）。
- Phase 12：对新 ACTIVE 归档自动调 `analyze_problem_with_tools` ——
  函数签名已是 `(db, problem_id) -> record`，后台循环直接可用。
- Phase 14：`tool_trace` + `input_snapshot` 双证据链已是审计记录形状。
