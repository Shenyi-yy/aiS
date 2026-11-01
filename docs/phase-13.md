# Phase 13: 人工确认 + 安全执行

## 目标

打通主线最后一环: **AI 建议 → 人工审批 → 白名单执行 → 结果回读落库**。
到本阶段为止, "监控→告警→取数→AI分析→故障定位→建议→人工确认→安全执行→
记录" 这条链里, 只有"执行"一节第一次允许系统触碰真实世界 —— 因此本阶段的
重点不是功能量, 而是**执行通道的结构性收紧**。

## 安全边界 (可核验的设计, 不是口头承诺)

| 约束 | 落地方式 | 可核验证据 |
|---|---|---|
| AI 永远不能执行 | AI 工具白名单 (8 项) 恒只读; /exec 路由不存在任何被 LLM 调用的路径 | `GET /ai/tools` + verify [6/6] 无执行动词 |
| 操作不可动态生成 | `exec_catalog.OPERATIONS` 是代码写死的静态元组, 无新增接口 | 源码 + `GET /exec/operations` |
| 动作只有三种 | `ALLOWED_ACTIONS = {restart, start, stop}`, Docker 引擎 API 固定形态, **无 shell、无 exec 端点** | `docker_exec.py` 全文 90 行 |
| 目标容器白名单 | `EXEC_ALLOW_TARGETS` (默认只 zops-drill); 不在名单的操作直接不可见 | `GET /exec/config` + verify [2/6] |
| 必须人工批准 | 唯一执行入口 `POST /exec/requests/{id}/approve`, 审批人留名; 建议/创建路径零执行 | 状态机 + verify [3/6] (未批准前容器仍 stopped) |
| 终态不可逆 | EXECUTED/FAILED/REJECTED 再审批 → 409, 防重复执行/否决被绕过 | verify [5/6] |
| 诚实落库 | 执行后回读容器状态 (restart→running), 达不到期望落 FAILED 带原文 | verify [4/6] + 单测 |

已知风险须知: compose 给 backend 挂了 `/var/run/docker.sock`, 拿到 socket
≈ 宿主机 root。收紧全部在代码层 (白名单+固定动作+审批), 若演示环境不放心,
删掉挂载行即可物理拔通道 —— 后端会诚实报 `socket_exists=false`, 批准接口
返 503, 其余功能不受影响。

## 关键设计决策

- **建议 = 单据, 不是任务**: 后台自动分析成功后, `ai_dispatcher.run_tick`
  调 `suggest_for_problem` 按触发器匹配目录, 只生成 `PENDING_APPROVAL`
  的 ExecutionRecord。去重按 (problem, op) 且含 REJECTED —— 人的否决必须
  被尊重, 后台不许换马甲再提。
- **同步执行**: approve 接口内直接调 Docker API 并回读状态 (秒级动作,
  不起后台任务), 请求返回即终局, 没有"执行中"这种模糊态。
- **执行前二次对质目录**: 创建到批准之间白名单可能收紧, `approve_request`
  重新校验 `visible_operations()`, 不信任单据里的冗余字段。
- **EXEC_ENABLED=false = 503 且单据留在 PENDING**: 不静默吞请求, 也不误标
  终态; 开关打开后仍可批准。
- **失败也留完整证据链**: 引擎报错时 approver/comment/error 全部持久化,
  这正是 Phase 14 审计日志的数据源。

## 新增/变更接口

- `GET  /api/v1/exec/config` — 开关/白名单/socket 存在性/待审批数
- `GET  /api/v1/exec/operations` — 当前环境真正可执行的操作目录
- `GET  /api/v1/exec/requests` — 审批单分页 (status/problem_record_id 过滤)
- `POST /api/v1/exec/requests` — 人工创建 (只产出 PENDING_APPROVAL)
- `POST /api/v1/exec/requests/{id}/approve` — 批准并执行 (404/409/502/503)
- `POST /api/v1/exec/requests/{id}/reject` — 否决 (终态)
- 新配置: `EXEC_ENABLED / EXEC_AUTO_SUGGEST / EXEC_ALLOW_TARGETS /
  EXEC_DOCKER_SOCKET / EXEC_TIMEOUT` (.env.example 有注释)
- 新表: `execution_records` (create_all 直接建, 无需补列)

## 验证要点 (verify_phase13.sh, 6 项)

[1] 0.13.0 + 执行层三件套入镜像; [2] 白名单结构证据 (核心 5 容器不在名单);
[3] **硬门禁**: stop drill → 自动分析出建议 → 容器必须仍是 exited (零擅自
执行); [4] 人工批准后真重启 + 问题自动转 RECOVERED; [5] 否决路径 + 终态
不可逆; [6] AI 工具白名单无执行动词等老闸门不放松。
注意 [6] 沿用 P12 教训: 检查 docker 数据面前 drill 必须已在跑并等
unsupported 归零, 否则必假阳性。**口径补充 (三跑)**: `item_unsupported`
只数容器系真实项; 每次 `--build` 后 LLD 会给新镜像 sha 新生 2 个必然
预处理失败的 `docker.image.created/size` 模板噪音 (P11 手工禁用过的同类),
它们单列进 `item_unsupported_image` 如实回显、不卡闸门 —— 镜像系本就
不在容器计数口径内, 健康度口径与之对称; 容器系任何 unsupported (含
P11 禁用的 usage_inactive 复活) 仍是红线。

## 常见错误及解决方法

| 现象 | 原因 | 处理 |
|---|---|---|
| [2/6] `socket 未挂进容器` | scp 了 compose 但没 `up -d backend` 重建容器 | `docker compose -p zops up -d backend` |
| [3/6] 有分析无建议 | EXEC_AUTO_SUGGEST=false, 或该问题已有旧单 (含 REJECTED) | 查 `/exec/config`; 新一轮演练是新问题所以必出单 |
| [3/6] 容器不是 exited | 上一段超时前 drill 被别的 start 拉起 | 重跑 (trap 会拉回, 下轮重新 stop) |
| [4/6] 批准 503 | EXEC_ENABLED=false | .env 打开后 `up -d backend` |
| [4/6] 批准 500 (首跑实录) | 镜像内 uvicorn 跑在非 root 的 zops 用户, docker.sock 属 root:docker, connect 即 PermissionError | `.env` 补 `DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)` 后 `up -d backend` (compose `group_add` 注入); 修复后此类异常折叠为 502+FAILED 单据留痕, verify [2/6] 有容器内实连探针提前拦截 |
| [4/6] 批准 502 + 单据 FAILED | socket 权限 (docker 组外) / 引擎报错 | 单据 error 字段有引擎原文; `docker logs zops-backend` |
| [4/6] 容器起了但问题不转 RECOVERED | 归档周期 60s + Zabbix 恢复检测延迟 | 多等一轮; 查触发器 25333 表达式 |
| 批准核心容器名报 409 | 目标不在 EXEC_ALLOW_TARGETS | 这是白名单在正常工作, 不要加核心容器 |

## 下一阶段接线 (Phase 14: 审计日志)

- `execution_records` + `ai_analysis_records.trigger_source/tool_trace/
  input_snapshot` 已是证据三件套, P14 补统一查询面与不可篡改呈现
  (谁、何时、依据哪条分析、批准了什么、结果如何)。
- DispatcherStat 的进程内计数持久化也归 P14。
