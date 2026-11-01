# Phase 2 — Zabbix Agent 宿主机真机监控

## 目标

Phase 1 里被监控对象是一个容器（`zabbix-agent`，容器网络内 DNS 寻址）。Phase 2 把监控延伸到
**Ubuntu 宿主机本身**：用 apt 原生安装 `zabbix-agent2`、systemd 管理，监控真实的虚拟机
（CPU / 内存 / 磁盘 / 网卡 / 进程），这是生产环境最常见的 Agent 部署形态。

## 架构变化

```
zabbix-server 容器 ──Passive(10050)──▶ 宿主机 zabbix-agent2 (ubuntu-host)
       ▲                                      │
       └──────Active(宿主机IP:10051, compose 已发布端口)◀──┘
```

- 容器 agent → 宿主机 agent：寻址从 Docker DNS 名变成 **宿主机 IP**，UI 接口 Type 从 DNS 改为 **IP**。
- docker-compose.yml 为 zabbix-server 增加了 `ports: 10051:10051`，供 agent2 的 ServerActive 回连。
- agent2 配置三要素：`Server=`（允许谁来取数，支持 CIDR，脚本自动放行 127.0.0.1/宿主机IP/docker网段/网关）、
  `ServerActive=`（反向连哪，**不能带 CIDR**）、`Hostname=`（必须与 UI Host name 一字不差）。
- Passive 请求从 server 容器发往宿主机 IP 时会被 docker SNAT 成网桥网关（如 172.18.0.1），
  所以 `Server=` 必须包含该网段，否则 agent2 拒绝并记 "connection refused from ..."。

## 验证清单（verify_phase2.sh 的 6 项）

1. 宿主机 `systemctl is-active zabbix-agent2`
2. 宿主机 10050 监听
3. server 容器内 `zabbix_get -s <宿主机IP> -k system.uname / vm.memory.size[available]` 拿到真实值
4. 宿主机 IP:10051 可连通（compose 端口发布生效）
5. 数据库 `hosts` 表存在 `host='ubuntu-host' and flags=0`
6. 该主机 `history + history_uint` 有真实数据行（Zabbix 7.0 数值按类型分表，items 表没有 lastvalue）

全部 PASS 后，UI `Monitoring -> Latest data` 选 `ubuntu-host` 应能看到宿主机真实指标。

## 排障速查（Phase 2）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| setup 脚本 wget 404 | Ubuntu 版本非 22.04/24.04 | 脚本已拦截并报错；核对 `/etc/os-release` 的 VERSION_ID |
| agent2 active 但 server 取不到数 | `Server=` 未放行 docker SNAT 来源 | `sudo grep '^Server=' /etc/zabbix/zabbix_agent2.conf`，应含 172.x 网段；重跑 setup 脚本 |
| agent2 日志 connection refused | ServerActive 里误填了 CIDR | ServerActive 只能 `IP:端口`；重跑 setup 脚本即可修正 |
| zabbix_get 超时 | 宿主机 IP 选错（hostname -I 多网卡取了次要 IP） | 用 UI 接口里填的同一个 IP 手动 zabbix_get 对比 |
| UI 主机红叉 "Zabbix agent interface is not available" | 接口 IP 填错或 Type 还是 DNS | 主机 -> Interfaces -> Edit -> Type=IP, 值=setup 脚本打印的宿主机 IP |
| 历史有数据但只有几行 | 刚注册, 部分 item 间隔 1-3 分钟 | 等 2-3 分钟再跑 verify |
| 10051 不通 | 旧 compose 未更新/未重建 | scp 新 docker-compose.yml 后 `docker compose up -d zabbix-server` |
| agent2 启动失败 + 配置出现重复 Server/ServerActive 行 | 旧版 set_conf 用 sed 整行替换，把文件里被注释的示例行也展开了；且 sed -i 重写后 conf 权限可能不再被 zabbix 用户可读 | 用已修正的 setup 脚本重跑（改为"删除全部旧行+末尾追加唯一一行"，并 chown root:zabbix / chmod 640）；仍失败看 `sudo journalctl -u zabbix-agent2 -n 30` |
| `cannot parse the "Server" parameter: address "172.18.0.1" specified more than once` | Server= 里网段 CIDR 和其内嵌网关 IP 同时出现，Zabbix 判为重复地址 | `Server=` 只写 `127.0.0.1,<宿主机IP>,<docker网段CIDR>`，不要再单独加网关；setup 脚本已自动去重 |
