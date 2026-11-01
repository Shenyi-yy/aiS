# ZOPS — 基于 Zabbix + Docker + Python + LLM Agent 的智能运维监控与故障分析平台

版本 0.16.0 | 开发环境: Windows (仅编辑代码/上传) | 部署环境: Ubuntu 22.04/24.04 + Docker Compose

## 一句话闭环

真实监控 → 告警归档 → AI Agent 取数分析 → 故障定位与建议 → 人工确认 → 安全执行 → 全量审计 → 知识沉淀 (RAG) → 再喂给下一次分析。
大盘 (`http://<Ubuntu_IP>:8000/`) 零假数据，全部面板来自真实 API。

## 服务架构

| 服务 | 容器名 | 端口 | 说明 |
|---|---|---|---|
| Zabbix Web | zops-zabbix-web | 8080→8080 | Admin 登录，告警引擎 UI |
| Zabbix Server | zops-zabbix-server | 10051(内网) | trigger / problem 源头 |
| PostgreSQL | zops-postgres | 内网 | Zabbix 元数据与历史 (值只在 history 系表) |
| Zabbix Agent | zops-zabbix-agent | 10050(内网) | 主机指标采集 |
| FastAPI backend | zops-backend | 8000→8000 | 业务 API + 大盘 + AI Agent + 审计 + 知识库 |

backend 内部: SQLite 业务库 (named volume `backend_data`, WAL + busy_timeout) 存 problem_records /
ai_analysis_records / exec_requests / audit_events / execution_records / knowledge_entries；
Zabbix API 调用统一走 `services/zabbix_client.py` 唯一出口。

## 16 Phase 交付路线 (全部通过验证门禁)

| Phase | 内容 | 验证脚本 |
|---|---|---|
| 1 | Docker Compose 基础设施 + /health | verify_phase1.sh |
| 2 | Agent 主机监控接入 | verify_phase2.sh |
| 3 | Trigger 规则 + problem 同步归档 | verify_phase3.sh |
| 4 | FastAPI 骨架 + 统一响应体 | verify_phase4.sh |
| 5 | Zabbix API 只读取数层 | verify_phase5.sh |
| 6 | 监控大盘 Dashboard | verify_phase6.sh |
| 7 | Docker 容器监控 (LLD 模板) | verify_phase7.sh |
| 8 | 故障模拟演练 (zops-drill) | verify_phase8.sh |
| 9 | AI Agent 接线 (LLM 配置) | verify_phase9.sh |
| 10-11 | Tool Calling (Zabbix/Docker/Host 只读工具) | verify_phase10.sh / 11 |
| 12 | 自动分析巡检 (analysis tick) | verify_phase12.sh |
| 13 | 人工确认执行 (审批单 + 真实 docker 执行) | verify_phase13.sh |
| 14 | Audit Log (sha256 哈希链, append-only GET 面) | verify_phase14.sh |
| 15 | RAG 知识库 (BM25 检索 + 闭环复盘沉淀) | verify_phase15.sh |
| 16 | 平台完善 (大盘闭环四面板 + 时间线 + 非破坏总闸) | verify_phase16.sh |

## 安全红线 (平台级约束)

- AI 侧 9 个工具 (`zbx_* / docker_* / host_* / kb_*`) 恒 `readonly=true`，模型无法经分析链路执行任何命令；
  执行只走 Phase 13 人工审批通道 (exec_requests → approve/reject → 白名单命令)。
- `/api/v1/audit*` 路由面只有 GET，哈希链 append-only，`/audit/verify` 重放校验。
- 知识库写入自动生成"闭环复盘"必须真实闭环 (ACTIVE 归档 + SUCCESS 分析 + 执行记录)，不足则 404/409，绝不编造复盘。
- 密码/API Key 只存 Ubuntu `/opt/zops/.env` (ZABBIX_PASSWORD / LLM_API_KEY)，不进聊天、git、scp。

## 快速开始 (Ubuntu)

```bash
cd /opt/zops
cp .env.example .env        # 填 ZABBIX_DB_PASSWORD / ZABBIX_PASSWORD / LLM_API_KEY
bash scripts/deploy.sh      # 一键: CRLF 清理 / 随机密码 / 构建 / 健康等待
```

部署后: Zabbix Web `http://<Ubuntu_IP>:8080` (Admin/zabbix，首登改密后同步进 .env)；
大盘 `http://<Ubuntu_IP>:8000/`；文档式验证每阶段一个脚本 `bash scripts/verify_phase16.sh`。

## Windows 开发注意

- `.gitattributes` 强制 LF; VS Code 设置 `"files.eol": "\n"`
- `.env` 不提交, 只提交 `.env.example`
- 本地测试: `cd backend && py -3.14 -m pytest tests -q` (系统 python3.9 依赖不齐会失败)
- 部署: scp 逐行传单文件 (PowerShell 逗号多源会静默失败)，VM 上 `ls -l` 核对字节数后再 `up -d --build`

## 文档索引

各阶段设计决策/验证口径/常见错误见 `docs/phase-*.md`；`scripts/verify_phase16.sh` 为非破坏端到端总闸，可反复运行。
