# Phase 6: Web Dashboard（监控大盘）

## 目标

在后端提供零构建链的运维监控大盘：一张原生 JS 单页展示真实主机与问题归档，
数据全部来自 Phase 4/5 已验证的接口，不引入任何前端框架与假数据。

## 架构决策

- **静态文件挂 `/`，注册在所有 API 路由之后**：Starlette 按注册顺序匹配，
  `/health` 与 `/api/v1/*` 先命中，`StaticFiles(html=True)` 只兜底剩下的路径，
  不会吞掉已通关的 Phase 1 健康检查（verify [2/6] 专门守这条）。
- **原生 JS + 单文件 index.html**：无 npm/构建链，镜像不用改（`COPY app ./app` 天然带上 static/）。
- **新增 `GET /api/v1/hosts`**：host.get（status=0 即 MONITORED）走
  `services/zabbix_client.fetch_hosts()` —— Zabbix API 仍只有这一个出口。
- 大盘 10s 轮询刷新 + "立即同步" 按钮复用 `POST /api/v1/problems/sync`，
  前端不写任何独立同步逻辑。

## 数据流

```
浏览器 :8000/
  ├─ GET /api/v1/system/info   → 头部状态 chip (db 真实 SELECT 1)
  ├─ GET /api/v1/hosts         → 主机卡片 (Zabbix host.get, 实时)
  ├─ GET /api/v1/problems      → 告警表格 (problem_records 归档)
  └─ POST /api/v1/problems/sync → 手动触发归档 (与后台 60s 任务同一条代码路径)
```

## 7.0 / 本阶段陷阱

| 陷阱 | 说明 |
| ---- | ---- |
| StaticFiles 抢先匹配 | 挂 `/` 若在 include_router 之前，/health 会 404/返回 HTML，docker healthcheck 直接挂容器 |
| host.get 的 status 是字符串 | "0"=MONITORED，不是 int；HostInfo.monitored 用 `== "0"` 判断 |
| 大盘时区 | started_at/recovered_at 是 UTC epoch，前端 `toISOString` 显示，别误当北京时间 |
| 验证 [3/6] 依赖登录 | hosts 接口失败先回查 Phase 5 的 zabbix/health，而非怀疑本阶段代码 |

## 验证

`bash scripts/verify_phase6.sh` → PASS=6 FAIL=0；
六项分别守：首页 HTML、/health 存活、hosts 真实主机（含 ubuntu-host）、
归档数据仍在、零假数据引用扫描、同步幂等。
