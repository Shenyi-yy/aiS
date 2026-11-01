# Phase 7: Docker 监控

## 目标

让 Zabbix 看见宿主机上的 Docker：agent2 内建 docker 检查项 + 官方模板
"Docker by Zabbix agent 2" 的 LLD 自动发现全部容器，后端聚合出
`GET /api/v1/docker/summary` 供大盘展示监控覆盖与收数健康度。

## 架构决策

- **零自研采集**：容器发现/指标全部走 Zabbix agent2 内建插件与官方模板，
  后端只做只读聚合 —— 保持"监控归 Zabbix，平台归 ZOPS"的分层。
- **API 唯一出口不变**：新增 `fetch_monitor_items()`（item.get search
  `docker.`）与 `fetch_docker_triggers()`（trigger.get search 描述）两个语义
  方法，JSON-RPC 细节仍只在 `zabbix_client.py`。
- **诚实计数**：`discovered_container_count` = item key 第一段参数（容器 UUID）
  去重数，即"真正建了监控项的容器"，不读 docker.sock、不猜容器状态。
- 破坏性演练（停容器看告警）留给 Phase 8 故障模拟，本阶段验证全程只读。

## 数据流

```
zabbix-agent2 (zabbix 用户在 docker 组) ── /var/run/docker.sock
        │  docker.version / docker.discovery(LLD) / docker.container.get[uuid,...]
        ▼
Zabbix Server ──模板"Docker by Zabbix agent 2"实例化 item+trigger──► PG
        ▼
backend /api/v1/docker/summary  ← item.get + trigger.get (只读聚合)
        ▼
大盘左列 "Docker 容器监控" 面板 (发现数/触发器数/异常项数)
```

## 权限模型

`usermod -aG docker zabbix` 使 agent2 进程可读 docker.sock —— docker 组等价
root 权限，实验环境接受；生产应改用只读 socket 代理或 rootless 观测方案。

## 陷阱表

| 陷阱 | 说明 |
| ---- | ---- |
| 组变更不生效 | usermod 后必须 `systemctl restart zabbix-agent2`（进程组在启动时固化） |
| 挂模板后久久无数据 | LLD 发现规则默认间隔较长，UI 里对 "Docker containers" 规则 **Execute now** 立即发现 |
| item.get 的 lastvalue/error | DB items 表没有这两列（Phase 5 实证），只能走 API —— 本阶段沿用 |
| trigger.get 搜索词 | search 对 description 子串匹配 ["docker","container"]，模板触发器英文描述才命中 |

## 验证

`bash scripts/verify_phase7.sh` → PASS=6 FAIL=0：
agent2 实测 docker.version、docker ps ground truth、新路由注册、
docker.* 项 ≥20、发现容器数 ≥ 运行容器数且触发器已实例化、
unsupported/error 全零。
