#!/usr/bin/env bash
# =============================================================
# Phase 8 取证 v3: 验证演练改走 system.run 路线是否可行
#   B0 = zabbix 用户本机直接 docker inspect (CLI 对照, 不经 agent)
#   B1 = 经 agent2 被动查询 system.run[...] (演练项将要用的键)
#   B2 = 采样主机上容器级 docker 项的 type (0=agent/2=active/8=dependent)
# 只读, 不制造故障, 可反复执行
# 运行位置: Ubuntu 宿主机, /opt/zops 下: bash scripts/drill_key_probe3.sh
# =============================================================
set -u
cd "$(dirname "$0")/.."

VM_IP=$(hostname -I | awk '{print $1}')

echo "== B0: zabbix 用户本机 docker inspect (期望输出 true) =="
sudo -u zabbix docker inspect -f '{{.State.Running}}' zops-drill

echo "== B1: 经 server 容器 zabbix_get 打 agent2 的 system.run (期望 true) =="
docker exec zops-zabbix-server zabbix_get -s "${VM_IP}" \
  -k 'system.run[docker inspect -f {{.State.Running}} zops-drill]'

echo "== B2: 主机上容器级 docker 项采样 3 条看类型与预处理 =="
docker exec -i zops-backend python3 - <<'PYEOF'
import json, os, urllib.request
URL = os.environ["ZABBIX_API_URL"]
def call(method, params, token=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if token: body["auth"] = token
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json-rpc"})
    r = json.load(urllib.request.urlopen(req, timeout=25))
    if "error" in r:
        e = r["error"]; raise SystemExit(method + " 失败: " + str(e["message"]) + " / " + str(e["data"]))
    return r["result"]
token = call("user.login", {"username": os.environ.get("ZABBIX_USERNAME", "Admin"),
                            "password": os.environ["ZABBIX_PASSWORD"]})
hid = call("host.get", {"output": ["hostid"], "filter": {"host": ["ubuntu-host"]}}, token)[0]["hostid"]
items = call("item.get", {"output": ["name", "key_", "type", "value_type", "lastclock"],
        "hostids": hid, "search": {"key_": "docker.container"},
        "sortfield": "key_", "limit": 3,
        "selectPreprocessing": ["type", "parameters"]}, token)
print("    采样数量: " + str(len(items)))
for i in items:
    print("    type=" + str(i["type"]) + " | " + str(i["key_"])[:95])
    print("        name=" + str(i.get("name")) + " lastclock=" + str(i.get("lastclock"))
          + " pp=" + str(i.get("preprocessing"))[:120])
call("user.logout", [], token)
PYEOF

echo "=============================================================="
echo "判读: B1 返回 true → 演练改走 system.run 路线, 贴 B0/B1/B2 全部输出"
echo "=============================================================="
