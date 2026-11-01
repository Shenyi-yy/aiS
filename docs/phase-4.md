# Phase 4 — FastAPI 业务后端骨架

## 目标

把 Phase 1 的"最小 /health"后端升级为分层业务骨架，为 Phase 5（Zabbix API
采集）、Phase 6（Web Dashboard）、Phase 12/14（AI 分析与审计）打地基：

- 目录分层：`api / db / schemas`，路由不再塞 main.py
- SQLAlchemy 2.0 + SQLite（独立于 Zabbix 的 PostgreSQL）
- `problem_records` 表：Phase 5 用来归档 Zabbix 真实告警（本阶段建表不灌数据）
- 真实自检接口 `GET /api/v1/system/info`（SELECT 1 探 DB，不写死 True）
- 统一响应体 `{code, message, data}`，`GET /api/v1/problems` 分页查询

## 架构

```
Ubuntu (:8000)                                  容器内
┌──────────────┐   HTTP    ┌──────────────────────────────────┐
│ curl / 前端   │ ────────► │ zops-backend (uvicorn, 非 root)   │
└──────────────┘           │  main.py ─ lifespan: init_db     │
                           │   ├─ /health          (根,兼容)   │
                           │   └─ /api/v1/*                   │
                           │       ├─ system/info             │
                           │       └─ problems                │
                           │  app/db ── SQLAlchemy ── SQLite   │
                           │              (命名卷 backend_data │
                           │               /app/data/app.db)  │
                           └──────────────────────────────────┘
```

数据流边界：Zabbix 的采集/告警数据永远留在 PostgreSQL + Zabbix 自己的表；
backend 的 SQLite 只存 ZOPS 平台自己的业务记录（问题归档、后续审计），
Phase 5 通过 Zabbix API（不是直连 Zabbix 数据库）把 problem 拉进
problem_records，保证解耦。

## 接口清单

| 方法 | 路径 | 说明 | 数据来源 |
| ---- | ---- | ---- | -------- |
| GET | /health | 存活检查，docker healthcheck 依赖，形状与 Phase 1 一致 | 进程状态 |
| GET | /api/v1/system/info | 服务/环境/版本/DB 真实连通性 | SELECT 1 实测 |
| GET | /api/v1/problems | 问题归档分页（status/severity/limit/offset 过滤） | SQLite，Phase 5 前为 0 条属正常 |

## 本阶段决定与陷阱备忘

1. **`/health` 保持根路径且响应形状不变** —— compose 的 healthcheck 和已通关的
   Phase 1 基础设施依赖它；业务接口才走 `/api/v1`。
2. SQLite 在 uvicorn 多线程下必须 `connect_args={"check_same_thread": False}`，
   否则第二个并发请求就抛 `SQLite objects created in a thread can only be used
   in that same thread`。
3. 表建在容器卷里（`backend_data:/app/data`）：`docker compose down`（不带 -v）
   数据保留；`down -v` 会连库一起删——Phase 5 前无所谓，之后要小心。
4. `tests/test_api.py` 在 import app 之前用环境变量把 DATABASE_URL 指到临时目录
   （Settings 是 lru_cache，先 import 后设 env 无效），测试不污染真实库。
5. 非 root 用户 `zops` 拥有 `/app/data`，建表才有写权限；若换成 root 跑过再切
   非 root，会出 `attempting to write a read-only database`。

## 验证

`bash scripts/verify_phase4.sh` → **PASS=6 FAIL=0** 才进 Phase 5。

## 下一阶段预告

Phase 5：Zabbix API 接入（`zabbix_api` 单例客户端集中在一个模块，禁止散落）+
problem 采集器把真实告警写入 problem_records。前置：把你改过的 Web 登录密码
填入 Ubuntu 侧 `/opt/zops/.env` 的 `ZABBIX_PASSWORD`（不落 Windows）。
