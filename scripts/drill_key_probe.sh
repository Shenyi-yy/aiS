#!/usr/bin/env bash
# =============================================================
# Phase 8 取证辅助: 触发 LLD 立即发现演练容器, 列出其数据项, 并用
# zabbix_get 实测每个键 (7.0 drule.get 无 key_ 字段, 故只用 druleid+name)
# 只读 + 触发发现, 不制造故障, 可反复执行
# 运行位置: Ubuntu 宿主机, /opt/zops 下: bash scripts/drill_key_probe.sh
# =============================================================
set -u
cd "$(dirname "$0")/.."

CID=$(docker inspect -f '{{.Id}}' zops-drill)
VM_IP=$(hostname -I | awk '{print $1}')
echo "演练容器 ID: ${CID}"

echo "==> [1/3] 对本机所有发现规则 Execute now"
docker exec -i zops-backend python3 - <<'PYEOF'
import json, os, urllib.request
URL = os.environ["ZABBIX_API_URL"]
def call(method, params, token=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if token: body["auth"] = token
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json-rpc"})
    r = json.load(urllib.request.urlopen(req, timeout=20))
    if "error" in r:
        e = r["error"]; raise SystemExit(method + " 失败: " + str(e["message"]) + " / " + str(e["data"]))
    return r["result"]
token = call("user.login", {"username": os.environ.get("ZABBIX_USERNAME", "Admin"),
                            "password": os.environ["ZABBIX_PASSWORD"]})
hid = call("host.get", {"output": ["hostid"], "filter": {"host": ["ubuntu-host"]}}, token)[0]["hostid"]
drs = call("drule.get", {"output": ["druleid", "name"], "hostids": hid}, token)
print("    发现规则数量: " + str(len(drs)))
for d in drs:
    print("    drule " + str(d.get("druleid")) + ": " + str(d.get("name", "?")))
ids = [d["druleid"] for d in drs]
if ids:
    call("drule.run", {"druleids": ids}, token)
    print("    已 Execute now")
call("user.logout", [], token)
PYEOF

echo "==> [2/3] 等 60s 让 LLD worker 建项"
sleep 60

echo "==> [3/3] 列出演练容器的数据项并逐个 zabbix_get 实测"
docker exec -i -e ZCID="${CID}" zops-backend python3 - > /tmp/w1.tsv <<'PYEOF'
import json, os, urllib.request
URL = os.environ["ZABBIX_API_URL"]
CID = os.environ["ZCID"]
def call(method, params, token=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if token: body["auth"] = token
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json-rpc"})
    r = json.load(urllib.request.urlopen(req, timeout=20))
    if "error" in r:
        e = r["error"]; raise SystemExit(method + " 失败: " + str(e["message"]) + " / " + str(e["data"]))
    return r["result"]
token = call("user.login", {"username": os.environ.get("ZABBIX_USERNAME", "Admin"),
                            "password": os.environ["ZABBIX_PASSWORD"]})
hid = call("host.get", {"output": ["hostid"], "filter": {"host": ["ubuntu-host"]}}, token)[0]["hostid"]
items = call("item.get", {"output": ["name", "key_", "value_type", "lastvalue", "error"],
        "hostids": hid, "search": {"key_": CID}, "limit": 100}, token)
for i in items:
    print(i["key_"] + "\t" + str(i.get("name", "?")) + "\t"
          + str(i.get("value_type")) + "\t" + str(i.get("lastvalue") or ""))
call("user.logout", [], token)
PYEOF

echo "    演练容器数据项数量: $(wc -l < /tmp/w1.tsv)"
echo "    键 <TAB> 名称 <TAB> 值类型 <TAB> 最新值:"
head -30 /tmp/w1.tsv | sed 's/^/    /'
echo "---- 用 VM_IP 接口逐个实测 (与演练脚本同一路径) ----"
awk -F'\t' '{print $1}' /tmp/w1.tsv | while read -r k; do
  printf "%s ->\n    " "${k}"
  docker exec zops-zabbix-server zabbix_get -s "${VM_IP}" -k "${k}" 2>&1 | head -2 | sed 's/^/    /'
done
echo "=============================================================="
echo "把以上全部输出贴回来 (重点是名称含 Running/State 的键的实测返回值)"
echo "=============================================================="
