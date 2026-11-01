# Phase 9 — LLM Agent 接入（真实大模型往返 + 分析归档）

## 目标

平台第一次"开口说话"：对一条**真实归档告警**（problem_records，来自 Phase 5
归档链路）组装证据包 → 调用大模型 → 把分析正文与证据快照一起落库。

```
GET  /api/v1/ai/config            LLM 三要素注入状态（只回显 Key 长度）
POST /api/v1/ai/check             真实最小往返（延迟毫秒）
POST /api/v1/ai/analyze/{id}      对归档问题 id 做一次故障分析
GET  /api/v1/ai/analyses          分析归档分页（SUCCESS/FAILED 都如实可查）
```

后端版本 0.7.0 → **0.9.0**。新增表 `ai_analysis_records`（启动时 create_all
自动建，老卷无损）。

## 设计决策

1. **OpenAI 兼容协议，供应商无关**："可以调用任何模型"的实现 =
   `llm_client.py` 只依赖 `POST {base_url}/chat/completions` +
   `Authorization: Bearer` 这套事实标准。DeepSeek / 通义 compatible-mode /
   Kimi / 智谱 / 自建 Ollama、vLLM 全兼容，**换供应商只改 .env 三个值
   （LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），代码零改动**。
   不装 openai SDK——requirements 里现成的 httpx 就够，VM 侧免新增依赖、
   免镜像源风险。
2. **凭据纪律延续**：Key 只进 Ubuntu 侧 `/opt/zops/.env` → compose 注入
   容器环境变量。`GET /ai/config` 只回显 `api_key_len`（长度），响应体
   结构上不可能泄漏 Key；日志只打模型名与 token 数。
3. **两个"唯一出口"并立**：Zabbix 证据回查走 `zabbix_client.fetch_event_detail`
   （新增语义方法，7.0 陷阱：`eventids` 是字符串数组），大模型调用走
   `llm_client`——路由层和 analyzer 都不直连任何外部 API。
4. **失败诚实**：LLM 调用失败**也落一行 FAILED**（含证据快照 + 错误原因）
   再抛 502。大盘/验证脚本永远分得清"AI 真说了话"和"AI 没说话"；
   验证脚本 [4/6] 要求正文 ≥80 字符且 [6/6] 要求查无此问题必须 404——
   杜绝模板假文。
5. **证据快照（input_snapshot）**：当时喂给模型的完整 JSON 落库。
   这是 Phase 14 审计链路的雏形：复盘时能回答"AI 是根据什么说的这话"。
6. **System prompt 硬约束**：只准依据证据、不得虚构指标数值、缺证据要
   明说缺什么、处置建议必须标注风险等级（本平台禁止 AI 直接执行命令，
   执行留到 Phase 13 人工确认）。

## 文件地图

```
backend/app/config.py                +LLM 四字段 (base_url/api_key/model/timeout)
backend/app/services/llm_client.py   新增: 全后端唯一直连大模型的模块
backend/app/services/zabbix_client.py +fetch_event_detail (AI 证据回查)
backend/app/services/ai_analyzer.py  新增: 证据组装→chat→落库 (FAILED 也落)
backend/app/db/models.py             +AIAnalysisRecord (ai_analysis_records)
backend/app/schemas/analysis.py      新增: LLMConfigOut/CheckResult/AnalysisOut/Page
backend/app/api/routes/analysis.py   新增: /api/v1/ai/*
backend/app/api/router.py            注册 analysis 路由
backend/app/version.py               0.9.0
backend/tests/test_ai.py             新增: 未配置兜底/证据链/FAILED 落库/404 边界
docker-compose.yml                   backend env +LLM_BASE_URL/KEY/MODEL/TIMEOUT
.env.example                         LLM 段补供应商示例
scripts/verify_phase9.sh             6 项验证 (真调用, 无假 PASS)
```

## Ubuntu 侧配置步骤（Key 绝不进聊天/Git/scp）

```bash
cd /opt/zops
nano .env        # 文末三行填你的供应商 (见 .env.example 注释里的 5 组示例)
# LLM_BASE_URL 一律填到 /v1 为止; 填错最常见是多了 /chat/completions
docker compose -p zops up -d --build backend
```

## 常见错误及解决

| 现象 | 原因 | 处理 |
|---|---|---|
| [1/6] version≠0.9.0 | 代码没 scp 或没 `--build backend` | scp 后 `docker compose -p zops up -d --build backend` |
| [2/6] configured=False | .env 没填 / 填了没重建容器 | env 只在容器重建时注入：`up -d backend`（非 restart） |
| [3/6] HTTP 401/403 | Key 错 / base_url 少 /v1 / 多 /v1 | 用 .env.example 注释里的示例逐字对照 |
| [3/6] HTTP 429 / 余额提示 | 供应商限流或欠费 | 换模型/充值,与平台无关 |
| [3/6] 网络失败 (name resolution) | 容器内出不去外网 | VM 若走代理需给 docker 配代理; 或改用国内可直连供应商 |
| [4/6] 空表提示 | problem_records 无数据 | `curl -X POST localhost:8000/api/v1/problems/sync` 或跑一轮 fault_drill.sh |
| [4/6] 502 "查无此事件" | 不会发生——Zabbix 回查失败只记 unavailable 缺口,不阻塞分析 | 若 analyze 502, detail 一定是 LLM 侧原因 |
| [6/6] unsupported/error≠0 | docker.kernel_mem* 被重新启用 (Phase 7 备忘) | UI 禁用这两项后重跑 |

## 与后续阶段的接线

- Phase 10 (Zabbix Tool Calling)：把 `zabbix_client` 的语义方法包装成
  function-calling 工具——`llm_client.chat` 的 messages 结构已兼容 tools 字段,
  届时只加不改。
- Phase 12 (AI 自动分析)：`ai_analyzer.analyze_problem` 已是完整闭环函数，
  届时在后台循环里对新 ACTIVE 归档自动调用即可。
- Phase 14 (审计)：`ai_analysis_records.input_snapshot` 是审计证据链第一环。
