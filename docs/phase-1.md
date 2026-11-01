# Phase 1 — Docker Compose + Zabbix 环境搭建

## 1. 本阶段目标

只做一件事：把监控底座跑起来，并且证明数据是真的。

| 组件 | 容器名 | 镜像 | 端口 |
|---|---|---|---|
| postgres | zops-postgres | `postgres:16-alpine` | 仅内网 |
| zabbix-server | zops-zabbix-server | `zabbix/zabbix-server-pgsql:7.0-ubuntu-latest` | 10051（内网） |
| zabbix-web | zops-zabbix-web | `zabbix/zabbix-web-nginx-pgsql:7.0-ubuntu-latest` | 8080→`ZABBIX_WEB_PORT` |
| zabbix-agent | zops-zabbix-agent | `zabbix/zabbix-agent:7.0-ubuntu-latest` | 10050（内网, passive） |
| backend | zops-backend | 本地构建（FastAPI） | 8000→`BACKEND_PORT` |

**明确不做**：Trigger 配置、Zabbix API 封装、Dashboard、AI Agent、RAG。
**不生产任何假数据**——Latest data 里的 CPU/内存全部来自 Agent 真实采集。

> Agent 采集的是容器自身可见的系统指标；监控 Ubuntu 宿主机真机是 Phase 2 的内容。

## 2. 验证清单（逐步）

1. `docker compose ps` — 5 个容器 Up；约 1-2 分钟后 postgres/zabbix-server/backend 显示 `(healthy)`。
2. `curl http://<Ubuntu_IP>:8000/health` → `{"status":"ok",...}`；`/docs` 可见 Swagger。
3. 浏览器 `http://<Ubuntu_IP>:8080` → Admin / zabbix 登录（首次强制改密码）。
4. 添加主机 `zabbix-agent`（见 docs/deployment.md 第三节），接口 DNS 值必须等于 `zabbix-agent`。
5. Monitoring → Latest data 过滤该主机，1-2 分钟内出现真实 CPU/内存/网卡数据 = 核心验收点。
6. `bash scripts/verify_phase1.sh` 全 PASS。
7. 可选：`docker logs zops-zabbix-server | head -30` 应含 `database is up and running`。

## 3. 排障速查

| 现象 | 原因 | 解决 |
|---|---|---|
| Web 报 Cannot connect to database / server 反复重启 | `.env` 带 CRLF（值尾部藏 `\r`）或直接把 Windows 的 .env 传上来 | Ubuntu 重新 `cp .env.example .env`；`cat -A .env` 见 `^M$` 即中招；deploy.sh 会自动清一次 |
| server 日志无限刷 "PostgreSQL server is not available" | **Zabbix 7.0 入口脚本只读 `POSTGRES_PASSWORD`，不认 `ZABBIX_DB_PASS`**（实测 /usr/lib/docker-entrypoint/pgsql.sh:43-47），缺该变量时拿默认密码 "zabbix" 认证失败 | server 服务的 environment 用 `POSTGRES_PASSWORD: ${ZABBIX_DB_PASSWORD}`；已踩坑改完直接 `docker compose up -d`，无需删卷 |
| Web 容器一直 health: starting，`curl :8080` 返回 000 | zabbix-web 的入口脚本同样要直连数据库生成 zabbix.conf.php，缺 DB 凭据就一直等待，nginx/php-fpm 不启动 | web 服务也要配 `DB_SERVER_HOST/POSTGRES_USER/POSTGRES_DB/POSTGRES_PASSWORD`（与 server 同一组值） |
| agent 崩溃重启 "error parsing the ServerActive parameter: address invalid" | `ZBX_SERVER_HOST` 会同时写入 `Server=` 和 `ServerActive=`，后者不支持 CIDR 网段写法 | `ZBX_SERVER_HOST` 只填容器名 `zabbix-server` |
| Latest data 全是 N/A (Unsupported item parameter...) | Host name ≠ agent `ZBX_HOSTNAME`，或接口写 IP 而非 DNS 名 `zabbix-agent` | 三者对齐：Host name 与接口 DNS 都填 `zabbix-agent`；`docker compose exec zabbix-agent grep -E '^Hostname=|^Server=' /etc/zabbix/zabbix_agentd.conf` |
| 查数据时 SQL 报 `column i.lastvalue / item_value / i.error does not exist` | **Zabbix 7.0 schema 变更**：items 表删掉了 lastvalue/error 列，真实数值按类型存在 `history`（浮点）/`history_uint`/`history_str`/`history_log`/`history_text`/`history_bin` | 统计真实数据用 `select count(*) from history h join items i using(itemid) join hosts hs using(hostid) where hs.host='xxx';`（verify_phase1.sh 已按此实现） |
| 502 / web 连不上 server | web 启动早于 server 就绪 | `docker compose restart zabbix-web` |
| 端口已占用 | 8080/8000 冲突 | 改 `.env` 的 `ZABBIX_WEB_PORT` / `BACKEND_PORT` 后 `docker compose up -d` |
| 容器反复重启 | 镜像拉取失败 / `ZABBIX_DB_PASSWORD` 未设置 | `docker compose logs <服务>` 看第一条 ERROR；compose 用 `:?` 强制校验过该变量 |
| postgres 卷权限报错 | 曾用 Windows 路径 bind mount | 本项目全命名卷，勿改 bind mount；已踩坑 `docker compose down -v` 重来 |
| permission denied Docker daemon | 不在 docker 组 | `sudo usermod -aG docker $USER` 后注销重登 |
| backend 构建失败 | pip 超时 / 文件没传全 | 重试 `docker compose build --no-cache backend`，可换清华源 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |

验证全部通过后，向 AI 助手反馈结果，进入 Phase 2：Zabbix Agent + Linux 宿主机监控。
