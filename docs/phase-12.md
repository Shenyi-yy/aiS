# Phase 12 — AI 自动故障分析（后台调度闭环）

## 目标

把 Phase 10/11 的"人点一下才分析"升级为主线的自动化段：

```
Zabbix 告警 → (P5 归档循环) problem_records ACTIVE
  → ai_dispatcher 周期扫描 "ACTIVE 且无分析记录" 的归档
  → analyze_problem_with_tools(trigger_source="AUTO")   ← P10/11 的 8 工具取证
  → SUCCESS/FAILED 落库, GET /api/v1/ai/auto 实时可观测
```

后端版本 0.11.0 → **0.12.0**。`ai_analysis_records` 新增 `trigger_source`
列（AUTO/MANUAL，启动时幂等 ALTER，老记录回填 MANUAL）。

## 设计决策

1. **同一问题只自动分析一次**：待办口径 = `ACTIVE 且不存在任何分析记录`。
   FAILED 也视为"处理过"——分析失败要不要再来一次是**人的决定**
   （手工 POST /ai/agent/{id} 随时可用，记 MANUAL），机器不重试、不烧额度、
   不制造重试风暴。
2. **三重限量防失控**：单轮最多 3 条（`AI_AUTO_MAX_PER_TICK`，防告警风暴
   烧穿 LLM 额度）+ 扫描周期 ≥30s + 每条仍受 P10 的 `LLM_AGENT_MAX_ROUNDS`
   轮数护栏约束。调度自身异常只记日志下一轮继续（与归档循环同款容错）。
3. **不装忙**：LLM 未配置时 `run_tick` 直接返回、**不推进任何计数**，
   状态端点如实显示 `llm_configured=False`；`AI_AUTO_ENABLED=false` 则
   lifespan 根本不拉起任务（日志说明原因）。
4. **AUTO/MANUAL 必须可区分**：`trigger_source` 是给 Phase 13（人工确认
   只处理 AUTO 产出的建议）和 Phase 14（审计"谁让模型跑的"）准备的证据,
   本阶段先把数据打对。
5. **运行计数只放内存**（ticks/analyzed_total 等，进程重启清零）：持久化
   审计是 P14 的正式范围，本阶段不偷偷建第二套账。

## 新接口 / 新配置

```
GET  /api/v1/ai/auto      调度状态: 开关/周期/限量/实时计数/当前待办数

AI_AUTO_ENABLED=true      总开关 (false 需 up -d backend 重建生效)
AI_AUTO_INTERVAL=60       扫描周期秒 (30-3600)
AI_AUTO_MAX_PER_TICK=3    单轮最多自动分析条数 (1-10)
```

## 文件地图

```
backend/app/services/ai_dispatcher.py   [新] find_pending/run_tick/循环+运行计数
backend/app/services/ai_agent.py        [改] analyze_problem_with_tools +trigger_source
backend/app/main.py                     [改] lifespan 拉起/取消 ai-auto-analyze 任务
backend/app/db/models.py                [改] AIAnalysisRecord +trigger_source 列
backend/app/db/base.py                  [改] _ensure_columns 幂等补列
backend/app/api/routes/analysis.py      [改] GET /ai/auto
backend/app/schemas/analysis.py         [改] +AutoStatus, AnalysisOut +trigger_source
backend/app/config.py                   [改] +ai_auto_* 三字段
backend/app/version.py                  [改] 0.12.0
backend/tests/test_dispatcher.py        [新] 6 个单测(口径/限量/不重排/不装忙)
docker-compose.yml / .env.example       [改] AI_AUTO_* 三项透传
scripts/verify_phase12.sh               [新] 6 项验证 + 内置 /health 就绪等待
```

## Ubuntu 操作要点

scp 后重建即可；`.env` 可不动（三项都有 compose 默认值）。注意你现场
`.env` 已有 `LLM_AGENT_MAX_ROUNDS=10`，保持不变即可。

```bash
cd /opt/zops && docker compose -p zops up -d --build backend
```

启动日志应出现 `已补列: ai_analysis_records.trigger_source`（仅首次）和
`AI 自动分析调度已启动 (interval=60s, max_per_tick=3)`。

## 常见错误及解决

| 现象 | 原因 | 处理 |
|---|---|---|
| [1/6] version≠0.12.0 | 没 `--build` 重建 | `docker compose -p zops up -d --build backend` 后等 `/health` 通 |
| [2/6] llm_configured=False | compose 注入丢失 | `GET /api/v1/ai/config` 对照 P9；查 `.env` LLM 三要素 |
| [2/6] enabled=False | `.env` 里 AI_AUTO_ENABLED=false | 按需求改回 true 后 `up -d backend` |
| [3/6] 缺列=<缺列> | `_ensure_columns` 未执行 | `docker logs zops-backend --tail 50` 看启动异常 |
| [4/6] 320s 没归档 | Zabbix 侧触发器/归档循环 | `docker logs zops-backend \| grep 归档`；触发器 25333 是否在 |
| [4/6] src=MANUAL/None | 后台没跑, 是你手工触发的那条 | 看 `/ai/auto` 的 ticks/errors_total；多半是轮数超限落了 FAILED → 加大 LLM_AGENT_MAX_ROUNDS |
| [5/6] 记录数>1 | 待办口径回归 | 核对 find_pending 的 not_in 子查询是否被改坏（单测会红） |
| 告警风暴后疯狂烧 token | 不可能: 三重限量 | 若属实贴 /ai/auto 输出回来排查 |

## 与后续阶段的接线

- Phase 13：人工确认执行只应消费 `trigger_source=AUTO` 的 SUCCESS 记录里
  的"建议"，把"分析→建议→人批准→执行"接上。
- Phase 14：`trigger_source` + `tool_trace` + `input_snapshot` 三列就是
  审计日志的证据三元组，届时补"谁在什么时候批准/执行了什么"。
