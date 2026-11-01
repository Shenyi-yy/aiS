# Phase 8 — 故障模拟（全链路闭环演练）

## 目标

第一次**受控破坏性**演练，验证平台的"心脏"能否在真实故障下闭环：

```
docker stop zops-drill                (制造故障, 只碰专用演练容器)
  → agent2 采集 docker.container_info.state.running["/zops-drill"] : 1 → 0
  → ZOPS演练 触发器 PROBLEM 事件                                        (随采集周期)
  → 后端归档 problem_records status=ACTIVE                               (≤60s, 演练中手动 sync 加速)
  → 大盘问题列表红色 ACTIVE 行
docker start zops-drill               (恢复)
  → recovery_expression 命中 → RECOVERY 事件 → 归档翻转 RECOVERED + recovered_at
```

后端代码零改动（版本保持 0.7.0）——全部复用 Phase 5 归档链路 + Phase 7 收数体系。

## 设计决策（probe1-4 取证后的最终形态）

1. **专用演练容器 `zops-drill`**：核心 5 容器（postgres/server/web/agent/backend）任何一个宕机都会把监控链路本身打瘸，演练失真。zops-drill 用本机镜像 `python:3.11-slim` 空转创建，Docker 模板 LLD 已自动为它建好 42 个监控项。
2. **不自建 item，触发器直接绑模板 LLD 项**（本阶段最大修正）。取证结论：
   - 7.0.31 agent2 的合法被动 docker 键只有 `docker.ping / docker.info / docker.containers / docker.container_info[...] / docker.container_stats[...]` 这一族；`docker.version`、`docker.container.get[...]`、`system.agent.type`、`system.run[...]`（被动）都**不是合法键**，`ZBX_NOTSUPPORTED: Unknown metric` 是正常拒答而非故障。
   - 容器级监控项是 7.0 新增 **type=18 "dependent agent item"（依赖型代理项）**，键形如 `docker.container_info.state.running["/zops-drill"]`——**参数是带斜杠的容器名，不是容器 ID**；用 zabbix_get 直查这类键永远失败，属预期。
   - probe4 实测：运行中 `state.running` 的 lastvalue=**1**（value_type=3 无符号整数，高频刷新）；文本项 `state.status`="running" 但刷新慢一个数量级，不选。
3. **触发器表达式**：问题 `last(/ubuntu-host/docker.container_info.state.running["/zops-drill"])=0`；恢复 `recovery_mode=1` + `last(...)=1`。整数比较不加引号。描述 `ZOPS演练:` 前缀，脚本按描述查重、幂等。
4. **stop/start 而非 rm**：**rm 会让 LLD 把 drill 的项连触发器依赖一起清掉，且 ACTIVE 态删除 = 事件永远无法恢复**——本阶段最大的坑。脚本内置保护：中途失败/Ctrl+C 自动 `docker start` 拉回演练容器。
5. **等待逻辑全部实证化**：不猜固定延时，轮询"API 里该项的 lastvalue"与"trigger.get 的 **value** 字段（7.0 陷阱：status 是启用标志恒 0，当前态在 value）"到达目标值才进下一步（超时 10/5 分钟），把 7.0 采集周期的不确定性交给轮询消化。

## 脚本

- `scripts/fault_drill.sh`：七步演练（[1] 前置检查 → [2] 确认采集项存在且=1 → [3] API 建触发器（幂等） → [4] stop 等 lastvalue=0 → [5] 等 PROBLEM+sync 归档 → [6] start 等 1+等 RECOVERED+归档翻转 → [7] 总结）。API 调用一律经 `docker exec -i zops-backend python3`（ZABBIX_API_URL 是 docker 内部服务名，宿主机解析不了）。
- `scripts/verify_phase8.sh`：五项只读证据核验：
  1. zops-drill Running=true 且 psql 查 `history_uint` 最新值=1（7.0 items 表无 lastvalue 列，真实值只在 history 系表；停止态已自愈的实证）；
  2. psql 联 triggers/functions/items——触发器绑的正是模板项，且旧设计自建 `docker.container.get%` item 残留 0 条；
  3. events 表交叉核对：每条 value=1 的 PROBLEM 的 r_eventid 均非 0（都有恢复指针）；
  4. 后端归档 RECOVERED≥1、无 ACTIVE、recovered_at>started_at；
  5. docker/summary unsupported=0 error=0、触发器≥1，两次 sync total 稳定（幂等）。

  通关标准 **PASS=5 FAIL=0**。

## Windows 操作（PowerShell，一条 scp 一个文件）

```powershell
scp C:\Users\YANGSHIYI\Desktop\aiS\zops\scripts\fault_drill.sh user@192.168.0.104:/opt/zops/scripts/
scp C:\Users\YANGSHIYI\Desktop\aiS\zops\scripts\verify_phase8.sh user@192.168.0.104:/opt/zops/scripts/
scp C:\Users\YANGSHIYI\Desktop\aiS\zops\docs\phase-8.md user@192.168.0.104:/opt/zops/docs/
```

## Ubuntu 操作

```bash
cd /opt/zops
ls -l scripts/fault_drill.sh scripts/verify_phase8.sh   # 先看大小/时间戳确认是新版本
bash scripts/fault_drill.sh        # 全程约 3-10 分钟, 走到 [7/7] 才算完
bash scripts/verify_phase8.sh      # PASS=5 FAIL=0 通关
```

## 验证方法

把两个脚本的完整终端输出贴回来。关键证据行：

- [4/7] 出现 `ITEM=51280|0|0|<时钟>`（停止态被 Zabbix 真实采集）；
- [5/7] 出现 `TSTAT=1`、`EVENT=...|value=1|r_eventid=0`、`ARCHIVED≥1`；
- [6/7] 出现 `TSTAT=0`、PROBLEM 行 `r_eventid` 不再是 0、`recovered_at 已回填: True`；
- verify_phase8.sh 末行 `PASS=5 FAIL=0`。

## 常见错误及解决

| 现象 | 原因 | 处理 |
|---|---|---|
| [1/7] drill 容器不存在 | 环境重建过 | 脚本会用本地 `python:3.11-slim` 自动补建, 等 1-2 分钟 LLD 发现后重跑 |
| [2/7] ITEM=none | LLD 清掉了 drill 的项（曾 rm 过容器） | UI Data collection → Discovery rules → Docker containers → Execute now，2 分钟后重跑 |
| API 报 `Not authorized` | 7.0 JSON-RPC 会话凭据必须随调用携带 | 脚本已统一走 `Authorization: Bearer` 头；自改脚本务必带上 |
| API 报 `Incorrect method "drule.run"` | 7.0 方法改名 | 用 `discoveryrule.run` |
| [4/7] 10 分钟值不变 0 | stop 后 container_info 主项周期未到 / agent 不可达 | 脚本自动保护性拉起；重跑前 UI 看该项 lastclock 是否在走 |
| [5/7] 触发器不进 PROBLEM（但 events 表其实有事件） | **7.0 `trigger.get` 的 `status` 是启用标志（恒 0），当前状态在 `value` 字段（0=OK 1=PROBLEM）**——盯错字段会误判"未触发" | 脚本已改读 `value`；佐证看 `lastchange` 是否随停/启动推进 |
| verify [3/5] psql 报 `column e.r_eventid does not exist` | **7.0 events 表没有 r_eventid 列，恢复指针在 `event_recovery` 表**（API 层 event.get 仍返回 r_eventid 字段，是联表拼的） | 脚本已改 `left join event_recovery`；`event_recovery` 也没有 clock 列 |
| psql 报 `relation "hosts_items" does not exist` | 主机-项关联在 7.0 仍是 `items.hostid` 直连 | 用 `from items i join hosts h on h.hostid=i.hostid` |
| DB 里触发器表达式变成 `{36316}=0` 形如乱码 | 7.0 按 **functionid** 重写表达式（functions 表 functionid→itemid 是绑定真身） | 判绑定用 `select functionid,itemid,name,parameter from functions where triggerid=...` |
| [6/7] 不回 OK | recovery_expression 未命中或容器没被重新采集 | UI 看 item 最新值是否回 1；确认没有 rm 容器 |
| verify [2/5] 触发器=0 | 触发器描述被改 | 描述必须是 `ZOPS演练: 容器 zops-drill 已停止`（脚本前缀 `ZOPS演练:`） |

## 演练后清理（可选，Phase 16 前保留现场更好演示）

触发器与模板项共存无害；`state.running` 项由 LLD 管理不要手工删；若要删演练容器，**只在触发器 OK（已恢复）状态时** `docker rm -f zops-drill`。

## Phase 7 遗留备忘（本阶段生效）

- 凡直连 Zabbix API 的脚本一律 `docker exec -i zops-backend python3`（API_URL 是 docker 内部名）。
- `docker.kernel_mem*` 两个探测项已禁用（新版 Docker 无该字段）；若 docker summary 又冒出 2 个 unsupported，先查它们是否被重新启用。
- verify_phase7.sh [1/6] 的 `grep -qi version` 属假 PASS（错误文本里自带 version），Phase 9 前收紧——本 Phase 起真实性判读基准就是 verify_phase8 的 psql 联查 + lastvalue 实证。
