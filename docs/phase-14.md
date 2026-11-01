# Phase 14: 审计日志 (Audit Log)

## 目标

把"监控→告警→取数→AI分析→建议→人工确认→安全执行"闭环里每一个关键动作
写进一条 **sha256 哈希链**（`audit_events` 表），提供只读查询、全量重放校验
与单问题时间线。**只追加**不是口号：服务层没有 update/delete 函数，路由层
没有写方法，openapi.json 可逐字核对。

## 核心设计

| 决策 | 做法 | 为什么 |
|---|---|---|
| 防篡改 | 每行 `row_hash = sha256(prev_hash + 本行规范化JSON)`，`prev_hash` 指向上一行签名，首行接 GENESIS(64个0) | 改内容→签名不符；删行/换序→prev 断节；`/audit/verify` 报第一个断链行 id |
| 时间戳哈希口径 | 参与哈希前规范化为 `%Y-%m-%dT%H:%M:%SZ`（UTC 整秒，naive 视为 UTC） | SQLite 回读丢 tzinfo，直接 isoformat 会与写入时哈希不一致 → 校验必炸 |
| detail 序列化 | 入库即规范化 JSON（键排序+紧凑分隔符），哈希直接取存储原文 | 消除序列化自由度；被改成非法 JSON 时重算签名对不上=抓到篡改，且校验端不会因 loads 炸掉 |
| 写入点失败处理 | `record_event` 吞掉自身全部异常只记日志；业务落库在前、审计追加在后 | 审计坏不能拖垮分析/审批；P13 教训反过来——执行异常必须留痕，但留痕机制自身不许成为新故障源 |
| 并发 | 模块级 `threading.Lock` 包住"读尾行→签名→插入" | 防链分叉（SQLite 提交虽串行，但读-算-写不是） |
| id 参与哈希 | 先 flush 拿自增 id 再签名 | 改主键/换序同样被重放抓到 |

## 写入点清单（本阶段接线）

| 事件 | 触发处 | source/actor |
|---|---|---|
| system.startup | main.lifespan | SYSTEM/system，detail 回显版本与全部开关 |
| analysis.success / analysis.failed | ai_analyzer（P9 手工）与 ai_agent（Agent, AUTO+MANUAL）| API 或 AUTO，detail 带 trigger_source/耗时/工具数 |
| dispatcher.tick | ai_dispatcher.run_tick（analyzed>0 或 errors>0 才记）| AUTO/dispatcher —— 还了 P12"STAT 只在内存"的债 |
| execution.request_created | execution.create_request（含 AUTO 建议）| AUTO/dispatcher 或 API/api-client |
| execution.approved | approve_request（EXECUTED/FAILED 都记）| API/审批人名 |
| execution.rejected | reject_request | API/审批人名 |

## 接口（全部只读）

- `GET /api/v1/audit` —— 分页，过滤 event_type / source(API|AUTO|SYSTEM) / link_problem_id
- `GET /api/v1/audit/verify` —— 全量重放：`{ok, checked, first_broken_id, reason}`
- `GET /api/v1/audit/timeline/problem/{id}` —— 问题归档 + 分析记录(证据快照) + 执行单据 + 关联事件，四要素一次拉齐

## 配置

`AUDIT_ENABLED`（默认 true）。false 只停止新写入，查询/校验照常——
**关审计不等于毁审计**。

## 与 P13 红线的关系

AI 侧 8 工具恒只读、执行只经人工审批通道的结构不变；审计层只是把
"谁在什么时候让模型分析/批准/执行了什么"变成可重放的证据流。
`tests/test_audit.py` 同时断言工具面与审计面无执行入口。

## 验证要点（verify_phase14.sh）

1. 0.14.0 + 三件套文件 + 三个 GET 路由注册
2. openapi 里 /audit 面零写方法 + 现有链重放 ok
3. system.startup 入链且 detail.version 对质
4. **硬门禁**：stop drill 后 AUTO 分析+建议事件当场入链时，容器必须仍是 exited
5. 人工批准 → execution.approved(actor=审批人) 入链 → 问题 RECOVERED
6. 全链重放 ok + 时间线四要素齐 + dispatcher.tick 持久化 + 容器系 unsupported=0（镜像系噪音单列不卡闸）

篡改检测不在 VM 真库演示（改真库会让链永久断），由单测在隔离库覆盖：
detail 改写→指认被改行；删行→指认下一行断链。

## 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| verify ok=False, first_broken_id=N | 链真断（人工改过库/换过库文件） | reason 会写明断因；确属误改则从备份库恢复，不能"重签洗白"（重签=毁证据） |
| /audit 全空但服务正常运行 | AUDIT_ENABLED=false 或旧镜像（scp 没 --build） | 看 system.startup detail 的 audit_enabled；重建 backend |
| [3/6] 无 startup 事件 | 重启前的旧事件在库里本来就有；刚重建且首轮失败 | docker logs zops-backend 看"审计事件写入失败"；多为权限/磁盘问题 |
| [4/6] 等不到 analysis.success 事件 | P12 链路问题（LLM Key/归档滞后）；或**陈旧遗留**：无审计时代（0.13.x 进程）分析过的 ACTIVE 老问题被误当本轮目标，它永远不会有新事件 | 脚本已改为只认 id>基线 的本轮新归档；对质 `select ... from ai_analysis_records where problem_record_id=<PID>` 的 created_at 是否早于本次容器启动 |
| 审计三类事件全缺但零报错日志 | 不是写失败——record_event 失败必打 "审计事件写入失败"；缺而不炸=写入调用没发生（陈旧数据或旧镜像） | 先 `docker exec zops-backend grep -c audit /app/app/services/ai_dispatcher.py`，再对质业务表时间戳与 system.startup 的 occurred_at |
| 并发写 "database is locked" | 归档循环/调度器/API 三类写者撞锁（默认 rollback-journal 长读会饿死写者） | 已加固：WAL+busy_timeout=30s（base.py 连接钩子）+ record_event SAVEPOINT 隔离与锁重试；审计失败绝不 rollback 调用方事务 |
| 时间线 404 | 问题 id 不存在 | 只组装真实归档，不补假数据 |
