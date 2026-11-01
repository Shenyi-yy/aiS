# Phase 5 — Zabbix API 接入与问题归档

## 目标

1. 后端通过 **Zabbix JSON-RPC API**（不是直连 Zabbix 数据库）完成：登录自检、
   拉取真实问题事件、恢复状态匹配、归档入库。
2. 全部 Zabbix API 调用集中在 `app/services/zabbix_client.py` 一个模块
   （项目硬性约定：API 调用不散落各文件）。
3. `problem_records` 表从"空表"变成"真实告警归档"：Phase 3 压测产生的
   `ZOPS: High CPU utilization` 等事件应能被同步进来。
4. 顺手诊断 Phase 3 遗留：磁盘手动 item `vfs.fs.size[/,pused]` 一直 0 数据。

## 数据流

```
Zabbix Server (PG: events/problem)
        │  user.login → token (缓存在客户端, 失效自动重登重试一次)
        ▼
services/zabbix_client.py  ←← 全项目唯一 API 出口
   │  event.get value=1 (PROBLEM, 近 N 天)      → 事件+主机名
   │  event.get value=0 (RECOVERY)              → {problem_eventid: 恢复时间}
   ▼
services/problem_sync.py  apply_events() 按 zabbix_eventid 幂等 upsert
   ACTIVE → RECOVERED 状态机; 重复同步不产生第二条
        ▼
SQLite problem_records  →  GET /api/v1/problems (Phase 6 Dashboard 数据源)
```

两种触发方式殊途同归（都调 `sync_once`）：
- 后台任务：lifespan 里 `asyncio.to_thread` 周期跑（ZABBIX_SYNC_INTERVAL，默认 60s）
- 手动：`POST /api/v1/problems/sync?days=30`

## 接口清单（新增/变化）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | /api/v1/zabbix/health | 真实 user.login + apiinfo.version；凭据错误 → 502（不含密码） |
| POST | /api/v1/problems/sync | 立即归档同步，返回 `{fetched, inserted, updated, recovered_matched}` |
| GET | /api/v1/zabbix/items | item 诊断（Phase 3 磁盘遗留），返回 Zabbix 原始字段 |
| GET | /api/v1/problems | 不变；Phase 5 起应能查到真实数据 |

## 凭据与安全

- `ZABBIX_USERNAME / ZABBIX_PASSWORD` 只存在于 **Ubuntu 侧 `/opt/zops/.env`**，
  由 docker-compose 注入容器环境变量 → pydantic-settings 读取。
- Windows 仓库里只有 `.env.example`（空密码占位）；密码不进代码、不进聊天、
  不进 git。`.env` 永不 scp。
- 密码为空时 backend 正常启动（其他 Phase 不受影响），Zabbix 接口返回 502 并
  给出中文提示。

## 本阶段陷阱备忘

1. Zabbix 7.0 的 `user.login` 返回 JWT 字符串，请求体用 `auth` 字段携带；
   会话失效报 "Not authorised"，客户端会自动重登重试一次。
2. 恢复事件（value=0）的 `related_eventid` 才指向问题事件，`objectid` 是
   triggerid——匹配错了会把 ACTIVE 误判成 RECOVERED。
3. DB 的 `items` 表没有 lastvalue/error 列（Phase 1 实证），但 **API 的
   item.get 返回里有这些字段**（API 层拼装的），所以诊断走 API 不走 SQL。
4. compose 注入的 `ZABBIX_PASSWORD` 若含 `$` 会被 compose 插值吃掉——
   .env 注释里已提醒；出问题先 `docker exec zops-backend env | grep ZABBIX_`。
5. 归档上限单次 500 条事件；实验环境够用，生产要改成增量游标（最后 eventid）。

## 验证

`bash scripts/verify_phase5.sh` → **PASS=6 FAIL=0** 才进 Phase 6。
另附一段不计分的 items 诊断 JSON（磁盘遗留问题），一起贴回来即可分析。

## 下一阶段预告

Phase 6：Web Dashboard —— 前端消费 `/api/v1/problems` 与 Zabbix Latest data，
展示主机状态/问题列表/时间线（真数据，接入前继续零 fake）。
