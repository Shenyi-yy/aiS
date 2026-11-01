# Phase 11 — Docker/Linux Tool Calling（只读白名单扩到 8 工具）

## 目标

在 Phase 10 的 Agent 循环上，把取证面从"只有 Zabbix 视角"扩展到
"容器 + 宿主机指标视角"：

```
POST /api/v1/ai/agent/{problem_id}
  → 8 个只读工具 (4 zbx_* + 3 docker_* + 1 host_*)
  → dispatch_tool 单闸门分发, 两组注册表之外一律回"不在白名单"
  → GET /api/v1/ai/tools 回显 8 个, readonly 恒 true
```

后端版本 0.10.0 → **0.11.0**。Phase 10 的表结构、接口、循环机制全部不动
（tool_trace 列已存在），本阶段纯增量。

## 设计决策

1. **不挂 docker.sock，数据面仍走 agent2→Zabbix**：新增的 3 个 docker 工具
   和 1 个 host 工具读的全是 Zabbix 已采集的 item 最新值（`item.get`），
   后端容器里没有、也永远不需要 docker.sock 或 shell。"不让 AI 直接执行
   危险命令"继续靠代码结构保证 —— 连执行通道都不存在。
2. **两组注册表 + 单闸门**：`host_tools.py` 与 `zabbix_tools.py` 形状一致
   （schema + handler + `execute_tool` 消化异常为 `{"error": ...}` 观察）；
   `ai_agent.dispatch_tool` 是唯一入口，双前缀之外名称直接返回白名单错误。
3. **容器清单口径**：`docker_list_containers` 用 LLD 建的
   `docker.container_info.state.running["/<name>"]` 键（7.0 铁律：running
   值在 history_uint，item.get 的 lastvalue 即最新上报），容器名从键反解。
4. **容器名注入防线**：`docker_container_detail` 先把名字过一遍
   `[^"\[\]]` 校验（拒绝 `"` 和 `]`，防键表达式注入），再交给
   `key_search=["/<name>"]` 做服务端匹配。
5. **host 指标取数策略**：`host_get_linux_metrics` 支持两种查法 ——
   按默认前缀组（system./vm./vfs. 各 15 条）或按关键字（≤50 条），
   item.get 的 `search` 多前缀是 AND 语义所以按前缀分次查询再去重合并。
6. **复用而不重复**：`docker_get_summary` 直接调 Phase 7 的
   `docker_monitor.build_summary`，聚合口径与大盘完全一致，避免两套真相。

## 工具清单（8 个，全部只读）

```
zbx_list_problem_events   列 PROBLEM 事件
zbx_get_event_detail      查单个事件详情
zbx_get_host_items        查主机监控项+最新值
zbx_get_active_triggers   查当前 PROBLEM 触发器
docker_get_summary        宿主机 Docker 聚合概览(容器数/异常项/触发器)
docker_list_containers    某主机全部被监控容器+运行状态(1/0)
docker_container_detail   单容器的全部 docker.* 监控项
host_get_linux_metrics    CPU/内存/文件系统类 Linux 指标
```

## 文件地图

```
backend/app/services/host_tools.py      [新] Docker/Linux 只读工具注册表
backend/app/services/ai_agent.py        [改] ALL_TOOL_SCHEMAS 合并两组 + dispatch_tool
backend/app/api/routes/analysis.py      [改] GET /tools 改读 ALL_TOOL_SCHEMAS
backend/app/version.py / main.py        [改] 0.11.0
backend/tests/test_agent.py             [改] +派发路由/坏容器名/host 组轨迹等用例
docker-compose.yml / .env.example       [不变]
scripts/verify_phase11.sh               [新] 6 项验证([5/6] 硬性要求轨迹含 host 组工具)
```

## 首轮验证排障（VM 实跑 PASS=5 FAIL=1 后修复）

[3/6] 三方核对抓到 `docker/summary` 聚合层的**两个真实 bug**（db=6、ps=6
都对，后端只报 4）：

1. **limit=300 截断**：`item.get` 按名称排序取前 300 条，而现场"主机启用项
   ~310 + 模板原型 ~44"超阈值，尾部容器的项被整体切掉。→ 默认口径提到
   1000（`fetch_monitor_items`/`build_summary`/路由 Query 三处同改）。
2. **容器计数正则匹配不到 7.0 键系**：7.0 Docker agent2 模板的容器级项是
   下划线键 `docker.container_info.*["/<name>"]`，旧 `_UUID_IN_KEY_RE` 的
   字符类不含 `_`，这类键一个都没算进 discovered_container_count。→ 新增
   `_NAME_PARAM_RE` 精确匹配名称参数系（UUID 系保留兼容），并跳过
   `{#宏}` 原型项防虚报。
3. 回归测试锁死口径：`test_summary_default_limit_no_container_truncation`
   （360 项 6 容器 + 1 原型必须报 6）+ `test_name_param_regex_matches_
   underscore_keys`（实证旧正则对真键全军覆没）。本地 34 passed。

**二轮验证追加发现（discovered=12 / unsupported=27→2）**：7.0 Docker 模板
还有**镜像发现系** `docker.image.*["sha256:<64hex>"]`（键无下划线，恰好
穿过旧 UUID 正则字符类），limit 放宽后进视野 → 6 真容器被数成 12。新增
`_is_image_param()` 剔除（键 `docker.image.` 开头或参数含 `sha256:`）；
其中 2 个镜像项 JSONPath 预处理永久失败（与 kernel_mem 同类，模板探测项
与本环境 Docker 输出不兼容），按先例运维侧禁用（item.update status=1）。

## Ubuntu 操作要点

代码 scp 后重建即可，**不需要动 .env、不需要迁移**：

```bash
cd /opt/zops && docker compose -p zops up -d --build backend
```

## 常见错误及解决

| 现象 | 原因 | 处理 |
|---|---|---|
| [1/6] host_tools=no | 没 `--build` 或 scp 漏文件 | 逐行 scp 后 `docker compose -p zops up -d --build backend` |
| [2/6] names 不足 8 | routes/analysis.py 或 ai_agent.py 是旧版 | 核对 `/api/v1/ai/tools` 返回，重新 scp |
| [3/6] bad db/summary/ps | Docker 模板 LLD 未更新，或 DB 口令非默认 | 看 Zabbix Web LLD 最近发现时间；DB 取不到会自动降级两方核对 |
| [5/6] zbx_only | 模型自己没选容器工具（合法但无增量证明） | 重跑一次；反复如此可加大 `LLM_AGENT_MAX_ROUNDS` |
| [4/6] 502 最大轮数 | 8 工具可选后取证路径变长 | .env 调 `LLM_AGENT_MAX_ROUNDS=8~10` 重建 |
| docker 工具全返 error | ubuntu-host 上 agent2 挂了/不在线 | `zabbix_agentd`/`agent2` 服务状态；`GET /api/v1/docker/summary` 手工验证 |

## 与后续阶段的接线

- Phase 12：自动分析调度直接复用 `analyze_problem_with_tools`，取证面已
  覆盖容器与宿主机指标。
- Phase 13：执行类工具届时以**独立白名单 + 人工确认**出现，与这 8 个只读
  工具严格分层；`dispatch_tool` 单闸门就是那时的插入点。
