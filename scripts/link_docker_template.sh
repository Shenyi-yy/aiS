#!/usr/bin/env bash
# =============================================================
# Phase 7 辅助: 把 "Docker by Zabbix agent 2" 模板挂到 ubuntu-host
# 并对所有 Docker 发现规则 Execute now (绕开 UI 模板弹窗的组过滤坑)
# 运行位置: Ubuntu 宿主机: bash scripts/link_docker_template.sh
# 执行方式: python 片段注入 zops-backend 容器内运行 —— .env 里的
#           ZABBIX_API_URL 是 docker 内部服务名, 宿主机无法解析,
#           只有容器网络内可达; 凭据也已在容器 env, 无需再读 .env。
# 幂等: 已挂模板不重复挂; 发现规则存在则再次触发立即发现
# 说明: 运维操作脚本(与 setup_host_agent.sh 同类), 不属于后端代码链路。
# =============================================================
set -euo pipefail

CONTAINER=${ZOPS_BACKEND_CONTAINER:-zops-backend}
if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  echo "[错误] 容器 ${CONTAINER} 未在运行 — docker compose up -d backend"
  exit 1
fi

docker exec -i "${CONTAINER}" python3 - <<'EOF'
import json, os, sys, urllib.request

URL = os.environ.get("ZABBIX_API_URL", "")
if not URL:
    sys.exit("[错误] 容器内没有 ZABBIX_API_URL 环境变量 (compose 注入缺失?)")
PASSWORD = os.environ.get("ZABBIX_PASSWORD", "")
if not PASSWORD:
    sys.exit("[错误] 容器内 ZABBIX_PASSWORD 为空 — 先修 .env 并重建 backend")

HOST_NAME = "ubuntu-host"
DOCKER_TEMPLATE = "Docker by Zabbix agent 2"


def call(method, params, token=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if token:
        body["auth"] = token
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json-rpc"},
    )
    r = json.load(urllib.request.urlopen(req, timeout=20))
    if "error" in r:
        e = r["error"]
        raise SystemExit(f"{method} 失败: {e['message']} / {e['data']}")
    return r["result"]


token = call("user.login", {
    "username": os.environ.get("ZABBIX_USERNAME", "Admin"),
    "password": PASSWORD,
})

tpls = call("template.get", {
    "output": ["templateid", "host"],
    "filter": {"host": [DOCKER_TEMPLATE, "Linux by Zabbix agent"]},
}, token)
print("[1] 找到模板:", [t["host"] for t in tpls])
if not any(t["host"] == DOCKER_TEMPLATE for t in tpls):
    raise SystemExit(
        f"[错误] 库里没有模板 '{DOCKER_TEMPLATE}' — 需从 UI Templates 页重新导入官方模板")

hosts = call("host.get", {
    "output": ["hostid", "host"], "filter": {"host": [HOST_NAME]},
}, token)
if not hosts:
    raise SystemExit(f"[错误] 主机 {HOST_NAME} 不存在")
hid = hosts[0]["hostid"]

# 7.0 host.get selectTemplates 返回字段不稳 (实测 KeyError 'templates');
# ubuntu-host 设计上有且仅有 Linux by Zabbix agent 一个模板,
# 故直接显式写入 [Linux, Docker] 两个模板 —— 重复设置同一链接是 no-op, 天然幂等。
want = sorted(t["templateid"] for t in tpls
              if t["host"] in (DOCKER_TEMPLATE, "Linux by Zabbix agent"))
call("host.update", {"hostid": hid,
     "templates": [{"templateid": i} for i in want]}, token)
print(f"[2] {HOST_NAME} 模板已设置为 {want} (Linux + Docker, 幂等)")

rules = call("drule.get", {
    "output": ["druleid", "name"], "hostids": hid, "search": {"name": "Docker"},
}, token)
print("[3] Docker 发现规则:", [r["name"] for r in rules] or "(无)")
if rules:
    call("drule.run", [r["druleid"] for r in rules], token)
    print("[4] 已对所有 Docker 发现规则 Execute now")
else:
    print("[4] 没有发现规则可触发 — 等 1-2 分钟让模板项落地后重跑本脚本")

call("user.logout", [], token)
print("完成: 等约 1 分钟后执行 bash scripts/verify_phase7.sh")
EOF
