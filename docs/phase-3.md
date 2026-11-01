# Phase 3 — Zabbix Trigger + Problems

## 目标

Phase 1/2 打通了"监控取数"。本阶段让数据变成**事件**：在 `ubuntu-host` 上创建 3 个 `ZOPS:` 前缀的
自定义触发器（Trigger），用真实 CPU 压测把问题（Problem）打出来，理解 Zabbix 的事件模型：

```
item 采集值 → trigger 表达式周期评估 → 超限生成 problem(事件) → 恢复条件满足自动 RECOVERED
```

不做：FastAPI（Phase 4）、Zabbix API（Phase 5）、通知渠道/AI。

## 三个触发器（表达式为 Zabbix 7.0 语法）

| 名称 | 表达式 | 阈值含义 | 严重性 |
| --- | --- | --- | --- |
| ZOPS: High CPU utilization on ubuntu-host | `avg(/ubuntu-host/system.cpu.util,5m)>70` | 5 分钟平均 CPU > 70% | Warning |
| ZOPS: Low available memory on ubuntu-host | `min(/ubuntu-host/vm.memory.size[available],5m)<300M` | 5 分钟内可用内存 < 300M | Average |
| ZOPS: Root filesystem almost full | `max(/ubuntu-host/vfs.fs.size[/,pused],5m)>85` | 根分区使用率 > 85% | Warning |

阈值自定（不复用模板内置触发器）是为了：①演练时可精确控制哪条会响；②`ZOPS:` 前缀方便脚本直查数据库验证。

## 演练原理

`trigger_drill.sh` 起 `nproc` 个 `yes > /dev/null` 进程压满所有核（真实负载），每 15 秒轮询
`problem` 表打印 ACTIVE/RECOVERED 状态；到时或被 Ctrl+C 后杀掉压测进程，CPU 回落，
无恢复表达式的触发器在条件不满足时自动恢复。`avg(...,5m)` 需要数据积累，一般 2-4 分钟出问题。

## 验证清单（verify_phase3.sh 的 4 项）

1. `triggers` 表存在 ≥3 个 `ZOPS:%` 且 status=0（Enabled）
2. 触发器经 `functions→items→hosts` 真实绑定到 `ubuntu-host` 的 item（防止表达式主机名写错导致表达式被禁用）
3. `problem` 表有 ZOPS 问题记录（ACTIVE 或 RECOVERED 均算，证明真响过）
4. `event` 表有对应事件流水（问题产生/恢复事件）

UI 侧人工确认：`Monitoring → Problems` 能看到 ZOPS 问题（恢复后到 `Monitoring → Problems → 时间范围/已恢复` 过滤可见）。

## 排障速查（Phase 3）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| verify [1] 找到 0 个 | 触发器名没以 `ZOPS:` 开头 / 建到了别的主机 | 触发器列表页核对名称前缀与 Host group |
| 触发器列表显示表达式 "Disabled"（红字） | 表达式里主机名或 item key 拼写与真实主机不符 | 表达式必须精确用 `/ubuntu-host/<key>`；改 Expression 重新保存 |
| 压测 5 分钟仍无 problem | item 更新间隔 1m + avg 窗口 5m，需要时间；或 CPU 阈值没达到 | `bash scripts/trigger_drill.sh 600` 拉长；看 Latest data 里 CPU 实际值 |
| problem 一直 ACTIVE 不恢复 | 压测进程没退出（脚本被 kill -9 而非 Ctrl+C） | `pkill -f 'yes'` 清掉残留，等 5 分钟自动恢复 |
| UI Problems 页看不到 | 默认只显示最近 24h 未恢复 | 右上角时间范围调大 / 勾选 Show closed |
| 建磁盘触发器报 `Incorrect item key "vfs.fs.size[/,pused]" ... on "ubuntu-host"` | 磁盘 item 由模板的 **Mounted filesystem discovery** LLD 规则自动生成，主机刚注册时第一轮发现还没跑，item 尚不存在 | Data collection → Hosts → ubuntu-host 行 → **Discovery** 链接 → 勾选 Mounted filesystem discovery → **Run now**，几秒后 item 出现再建触发器 |
| Run now 没反应 / LLD 规则永远 NEVER（本项目实测） | 7.0 模板的文件系统发现是**依赖型**规则 `vfs.fs.dependent.discovery`，其主 item 未随主机创建出来，规则断粮永远不出数；7.0 items 表也没有 `state` 列可查 unsupported | 绕开 LLD：手动建普通 item（Type=Zabbix agent, Key=`vfs.fs.size[/,pused]`, Units=%, 间隔 1m），agent 原生支持该 key（zabbix_get 实测返回 31.144844），触发器表达式不变 |
| verify [4] 查 `event.name` 无结果 | 7.0 event 表列结构与旧版教程不符 | verify_phase3.sh 已改为 `event join triggers on triggerid=objectid` 按名称前缀计数，不依赖 event.name |
| 数据库查询报列不存在 | Zabbix 7.0 中触发器名在 `triggers.description`，表达式在 `triggers.expression` | 本脚本已按 7.0 schema 写好，勿照搬 5.x/6.x 教程 SQL |
