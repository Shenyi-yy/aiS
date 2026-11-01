#!/usr/bin/env bash
# =============================================================
# Phase 8 探针4: 盘点 zops-drill 容器在 ubuntu-host 上的全部
# docker.container_info.*["/zops-drill"] 监控项, 找出"状态类"
# 项的精确键名 + 当前值 (fault_drill.sh 触发器表达式要用它)。
# 运行位置: Ubuntu 宿主机, /opt/zops 下: bash scripts/drill_key_probe4.sh
# 只读脚本: 不做任何 create/update, 可反复执行。
# =============================================================
set -euo pipefail

cd /opt/zops
set -a
source .env
set +a

echo "== C1: drill 容器全部相关项 (按键名排序) =="
docker exec -i zops-backend python3 - <<'PYEOF'
import json, os, urllib.request

API = os.environ["ZABBIX_API_URL"].rstrip("/") + "/api_json.php"
TOKEN = [""]  # 7.0: 登录后后续调用必须带 Bearer 头, 否则 "Not authorized"

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "method": method,
                       "params": params, "id": 1}).encode()
    headers = {"Content-Type": "application/json-rpc"}
    if TOKEN[0] and method != "user.login":
        headers["Authorization"] = "Bearer " + TOKEN[0]
    req = urllib.request.Request(API, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    if "error" in d:
        raise SystemExit("API错误[%s]: %s %s" % (method,
            d["error"].get("message"), d["error"].get("data", "")))
    return d["result"]

TOKEN[0] = rpc("user.login", {"username": os.environ["ZABBIX_USERNAME"],
                              "password": os.environ["ZABBIX_PASSWORD"]})
hosts = rpc("host.get", {"filter": {"host": ["ubuntu-host"]},
                         "output": ["hostid"]})
if not hosts:
    raise SystemExit("找不到主机 ubuntu-host")
hid = hosts[0]["hostid"]

items = rpc("item.get", {
    "output": ["itemid", "name", "key_", "value_type", "state",
               "lastvalue", "lastclock", "master_itemid"],
    "hostids": hid,
    "search": {"key_": "/zops-drill"},
    "sortfield": "key_",
    "limit": 120,
})
print("drill 相关项总数: %d" % len(items))

# 主项(被依赖的 agent 项)单独回查, 拿到它的键名
master_ids = sorted({i["master_itemid"] for i in items
                     if i.get("master_itemid") and i["master_itemid"] != "0"})
masters = {}
if master_ids:
    for m in rpc("item.get", {"output": ["itemid", "name", "key_", "type",
                                         "lastvalue", "lastclock"],
                              "itemids": master_ids}):
        masters[m["itemid"]] = m

print("")
print("---- 全部项清单 ----")
for i in items:
    lv = i.get("lastvalue") or ""
    lv = lv[:70] + ("..." if len(lv) > 70 else "")
    ms = masters.get(i.get("master_itemid"), {})
    print("itemid=%s | vt=%s | state=%s | lastclock=%s" % (
        i["itemid"], i["value_type"], i["state"], i["lastclock"]))
    print("    key : %s" % i["key_"])
    print("    name: %s" % i["name"])
    print("    val : %s" % lv)
    if ms:
        print("    主项: %s = %s" % (ms.get("key_", "?"),
                                     (ms.get("lastvalue") or "")[:80]))

print("")
print("---- C2: 状态类候选 (名称或键含 state/status/running) ----")
found = 0
for i in items:
    hay = (i["name"] + " " + i["key_"]).lower()
    if "state" in hay or "status" in hay or "running" in hay:
        found += 1
        print('>>> itemid=%s key=%s' % (i["itemid"], i["key_"]))
        print('>>> name=%s' % i["name"])
        print('>>> value_type=%s lastvalue=[%s]' % (i["value_type"],
                                                    i.get("lastvalue") or ""))
        ms = masters.get(i.get("master_itemid"), {})
        if ms:
            print('>>> 主项 %s 当前值: [%s]' % (ms.get("key_", "?"),
                                                ms.get("lastvalue") or ""))
        print("")
if found == 0:
    print("!!! 没匹配到状态类项, 请把上面 C1 清单整段贴回来")

try:
    rpc("user.logout", [])
except Exception:
    pass  # 清理性登出, 失败不影响结果
PYEOF

echo ""
echo "=============================================================="
echo "判读: C2 段 >>> 行就是 fault_drill 触发器要绑的状态项。"
echo "预期: 至少 1 个 value_type=4(文本) 的项, 运行中的 lastvalue"
echo "如 running/1/true 之类。把本脚本完整输出贴回来即可。"
echo "=============================================================="
