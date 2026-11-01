#!/usr/bin/env bash
# =============================================================
# Phase 8 取证 v2: 找 Docker LLD 规则真实归属 + ubuntu-host 310 个
# docker 项的构成; 发现规则若存在则用正确方法 discoveryrule.run 触发
# 只读 + 触发发现, 不制造故障, 可反复执行
# 运行位置: Ubuntu 宿主机, /opt/zops 下: bash scripts/drill_key_probe2.sh
# =============================================================
set -u
cd "$(dirname "$0")/.."

docker exec -i zops-backend python3 - <<'PYEOF'
import collections
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

print("== A1: ubuntu-host 当前挂载的模板 ==")
h = call("host.get", {"output": ["hostid"], "filter": {"host": ["ubuntu-host"]},
        "selectParentTemplates": ["host"]}, token)[0]
hid = h["hostid"]
for t in h.get("parentTemplates", []):
    print("    template: " + str(t.get("host")))

print("== A2: 全库范围找 Docker/container 相关发现规则 ==")
drs = call("drule.get", {"output": ["druleid", "name"], "search": {"name": ["Docker"]},
        "selectHosts": ["host"], "limit": 30}, token)
drs += call("drule.get", {"output": ["druleid", "name"], "search": {"name": ["ontainer"]},
        "selectHosts": ["host"], "limit": 30}, token)
seen = set()
todo = []
for d in drs:
    did = str(d.get("druleid"))
    if did in seen:
        continue
    seen.add(did)
    hosts = d.get("hosts", []) or []
    hs = ",".join([str(x.get("host")) for x in hosts])
    if hs == "ubuntu-host":
        todo.append(d["druleid"])
    print("    drule " + did + ": " + str(d.get("name")) + " @ " + hs)
if todo:
    call("discoveryrule.run", {"druleids": todo}, token)
    print("    已用 discoveryrule.run 触发 ubuntu-host 的规则: " + str(todo))
else:
    print("    [注意] ubuntu-host 名下没有 Docker LLD 规则")

print("== A3: ubuntu-host 上 docker.* 项构成 ==")
items = call("item.get", {"output": ["name", "key_", "value_type", "lastvalue"],
        "hostids": hid, "search": {"key_": "docker."}, "limit": 500}, token)
param = [i for i in items if "[" in i["key_"]]
print("    docker.* 总数: " + str(len(items)) + " ; 键含 [ 参数(容器级/LLD 风格)的: " + str(len(param)))
for i in param[:15]:
    print("    " + i["key_"][:110] + " | " + str(i.get("name"))[:36] + " | last=" + str(i.get("lastvalue"))[:30])

print("== A4: ubuntu-host 全部数据项按键名前缀分布 (前 15) ==")
allit = call("item.get", {"output": ["key_"], "hostids": hid, "limit": 800}, token)
pref = collections.Counter([i["key_"].split("[")[0] for i in allit])
print("    主机数据项总数: " + str(len(allit)))
for k, c in pref.most_common(15):
    print("    " + k + "  x" + str(c))

call("user.logout", [], token)
PYEOF

echo "=============================================================="
echo "把以上 A1-A4 全部输出贴回来"
echo "=============================================================="
