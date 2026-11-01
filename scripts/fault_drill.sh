#!/usr/bin/env bash
# =============================================================
# Phase 8: 故障演练 —— 全链路闭环首次验证 (破坏性, 但只碰专用演练容器)
# 运行位置: Ubuntu 宿主机, 在 /opt/zops 下执行: bash scripts/fault_drill.sh
#
# 设计 (probe4 实测修正: 不再自建 item、不用 system.run):
#   直接对 Docker 模板 LLD 已建好的
#   docker.container_info.state.running["/zops-drill"]
#   (无符号整数, 运行中实测值=1) 建触发器:
#     问题: last(...)=0   恢复: recovery_mode=1 + last(...)=1
#   链路: docker stop → 采集值 0 → Zabbix [PROBLEM] → 后端 sync 归档
#         → docker start → 采集值 1 → [RECOVERED] → 归档 recovered_at 回填
#
# 红线: 只操作 zops-drill; 核心 5 容器 (postgres/server/web/agent/backend) 绝不碰。
#       中途失败或 Ctrl+C 会自动 docker start 把演练容器拉回运行态。
# 幂等: 触发器按描述 "ZOPS演练:" 查重, 可反复执行。
# =============================================================
set -uo pipefail

cd /opt/zops
DRILL=zops-drill
BASE="http://127.0.0.1:8000"

api() {  # 在 zops-backend 容器内调 Zabbix API (宿主机解析不了服务名), $1=模式
  docker exec -i zops-backend python3 - "$@" <<'PYEOF'
import json, os, sys, urllib.request

MODE = sys.argv[1]
API = os.environ["ZABBIX_API_URL"].rstrip("/") + "/api_json.php"
HOST = "ubuntu-host"
KEY = 'docker.container_info.state.running["/zops-drill"]'
DESC = "ZOPS演练: 容器 zops-drill 已停止"
TOKEN = [""]

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "method": method,
                       "params": params, "id": 1}).encode()
    headers = {"Content-Type": "application/json-rpc"}
    if TOKEN[0] and method != "user.login":
        headers["Authorization"] = "Bearer " + TOKEN[0]   # 7.0 会话凭据
    req = urllib.request.Request(API, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    if "error" in d:
        raise SystemExit("API错误[%s]: %s %s" % (method,
            d["error"].get("message"), d["error"].get("data", "")))
    return d["result"]

TOKEN[0] = rpc("user.login", {"username": os.environ["ZABBIX_USERNAME"],
                              "password": os.environ["ZABBIX_PASSWORD"]})
hosts = rpc("host.get", {"filter": {"host": [HOST]}, "output": ["hostid"]})
if not hosts:
    raise SystemExit("找不到主机 " + HOST)
hid = hosts[0]["hostid"]

def find_item():
    for it in rpc("item.get", {
            "output": ["itemid", "key_", "value_type", "state",
                       "lastvalue", "lastclock"],
            "hostids": hid,
            "search": {"key_": "docker.container_info.state.running"},
            "limit": 50}):
        if it["key_"] == KEY:
            return it
    return None

if MODE == "item":
    it = find_item()
    if not it:
        print("ITEM=none"); raise SystemExit(1)
    print("ITEM=%s|%s|%s|%s" % (it["itemid"], it["state"],
                                 it["lastvalue"], it["lastclock"]))
elif MODE == "trigger_ensure":
    it = find_item()
    if not it:
        print("ITEM=none"); raise SystemExit(1)
    ex = 'last(/%s/%s)=0' % (HOST, KEY)
    rex = 'last(/%s/%s)=1' % (HOST, KEY)
    old = rpc("trigger.get", {"hostids": hid,
                              "search": {"description": "ZOPS演练:"},
                              "output": ["triggerid", "description"]})
    if old:
        print("TRIGGER=%s|exists" % old[0]["triggerid"])
    else:
        tr = rpc("trigger.create", {
            "description": DESC, "expression": ex,
            "recovery_mode": 1, "recovery_expression": rex,
            "priority": "4",
            "tags": [{"tag": "zops", "value": "phase8-drill"}]})
        print("TRIGGER=%s|created" % tr["triggerids"][0])
elif MODE == "trigger_status":
    # 陷阱实证: 7.0 trigger.get 的 status=启用标志(恒0), 当前态在 value (0=OK 1=PROBLEM)
    res = rpc("trigger.get", {"triggerids": [sys.argv[2]],
            "output": ["triggerid", "value", "lastchange"]})
    if not res:
        print("TSTAT=none"); raise SystemExit(1)
    print("TSTAT=%s|%s" % (res[0]["value"], res[0]["lastchange"]))
elif MODE == "events":
    # 7.0 实证: event.get 的 source/object/value 都是整数, 不收数组
    res = rpc("event.get", {
        "source": 0, "object": 0, "objectids": [sys.argv[2]],
        "output": ["eventid", "clock", "value", "r_eventid"],
        "sortfield": "eventid", "sortorder": "DESC", "limit": 6})
    for e in res:
        print("EVENT=%s|clock=%s|value=%s|r_eventid=%s" % (
            e["eventid"], e["clock"], e["value"], e.get("r_eventid", "0")))
    if not res:
        print("EVENT=none")
PYEOF
}

wait_item() {  # $1=期望 lastvalue(0/1), $2=超时秒; 成功时打印 ITEM 行
  local end=$((SECONDS + $2)) line lv
  while [ "$SECONDS" -lt "$end" ]; do
    line=$(api item 2>/dev/null | grep '^ITEM=' | tail -1) || true
    lv=$(echo "$line" | cut -d'|' -f3)
    if [ "$lv" = "$1" ]; then echo "$line"; return 0; fi
    echo "    等待采集刷新... 当前: ${line:-无}" >&2
    sleep 10
  done
  return 1
}

wait_trigger() {  # $1=期望 status, $2=triggerid, $3=超时秒
  local end=$((SECONDS + $3)) line st
  while [ "$SECONDS" -lt "$end" ]; do
    line=$(api trigger_status "$2" 2>/dev/null | grep '^TSTAT=' | tail -1) || true
    st=$(echo "$line" | sed 's/TSTAT=//' | cut -d'|' -f1)
    if [ "$st" = "$1" ]; then echo "$line"; return 0; fi
    echo "    等待触发器评估... 当前: ${line:-无} (0=正常 1=问题)" >&2
    sleep 10
  done
  return 1
}

STOPPED=0
restore_drill() {
  if [ "${STOPPED}" = 1 ]; then
    echo "[保护性拉起] 演练容器处于停止态, 正在 docker start ${DRILL} ..."
    docker start "${DRILL}" >/dev/null 2>&1 || true
    STOPPED=0
  fi
}
trap 'echo ""; restore_drill; exit 130' INT

echo "==> [1/7] 前置检查"
if ! docker inspect "${DRILL}" >/dev/null 2>&1; then
  if docker inspect python:3.11-slim >/dev/null 2>&1; then
    echo "    ${DRILL} 不存在, 用本地镜像创建 (不联网):"
    docker run -d --name "${DRILL}" --restart unless-stopped \
      python:3.11-slim sh -c 'while :; do sleep 5; done'
    echo "    已创建并启动。等 LLD 发现新容器 (1-2 分钟), 然后重跑本脚本。"
    exit 0
  fi
  echo "    [错误] ${DRILL} 与 python:3.11-slim 镜像都不存在, 无法演练。"
  exit 1
fi
if [ "$(docker inspect -f '{{.State.Running}}' "${DRILL}")" != "true" ]; then
  echo "    [错误] ${DRILL} 未运行, 先执行: docker start ${DRILL}"
  exit 1
fi
if [ "$(docker inspect -f '{{.State.Running}}' zops-backend)" != "true" ]; then
  echo "    [错误] zops-backend 未运行, 归档链路断。docker start zops-backend 后重试。"
  exit 1
fi
if ! curl -sf --max-time 5 "${BASE}/health" >/dev/null; then
  echo "    [错误] 后端 ${BASE}/health 不可达"; exit 1
fi
if ! systemctl is-active --quiet zabbix-agent2; then
  echo "    [错误] zabbix-agent2 未运行"; exit 1
fi
echo "    drill/backend/agent2/健康检查 全部就绪 [OK]"

echo "==> [2/7] 确认状态采集项存在且运行值=1"
LINE=$(api item | grep '^ITEM=' | tail -1) || true
if [ -z "$LINE" ]; then
  echo "    [错误] 找不到 state.running 项 (probe4 时 drill 应有 42 项)。"
  echo "    处置: UI Data collection → Discovery rules → Docker containers → Execute now, 2 分钟后重跑。"
  exit 1
fi
LV=$(echo "$LINE" | cut -d'|' -f3)
echo "    ${LINE}  (itemid|state|lastvalue|lastclock)"
[ "${LV}" = "1" ] || { echo "    [错误] 容器明明在运行但采集值=${LV}≠1, 贴本行输出排查。"; exit 1; }
echo "    [OK] 采集正常: 运行中 lastvalue=1"

echo "==> [3/7] 创建触发器 (只建触发器, 不建 item, 幂等)"
TLINE=$(api trigger_ensure | grep '^TRIGGER=' | tail -1) || true
TID=$(echo "$TLINE" | sed 's/TRIGGER=//' | cut -d'|' -f1)
[ -n "$TID" ] || { echo "    [错误] 触发器操作失败: ${TLINE}"; exit 1; }
echo "    ${TLINE} triggerid=${TID}"
echo "    问题式: last(/ubuntu-host/docker.container_info.state.running[\"/${DRILL}\"])=0"
echo "    恢复式: last(同上)=1  (recovery_mode=1)"

echo "==> [4/7] 停掉 ${DRILL}, 等待采集值变 0"
docker stop "${DRILL}" >/dev/null
STOPPED=1
if ! RES=$(wait_item 0 600); then
  echo "    [错误] 10 分钟采集值未变 0 → 已保护性拉起容器。贴 [2/7]-[4/7] 输出排查。"
  restore_drill; exit 1
fi
echo "    ${RES} [OK] 停止态已被 Zabbix 真实采集 (演练生效)"

echo "==> [5/7] 等待 Zabbix [PROBLEM] 并触发后端归档"
if ! TL=$(wait_trigger 1 "$TID" 300); then
  echo "    [错误] 触发器 5 分钟未进 PROBLEM → 已保护性拉起容器。贴输出排查。"
  restore_drill; exit 1
fi
echo "    ${TL} [OK] Zabbix 侧 [PROBLEM] (status=1)"
api events "$TID" | sed 's/^/    /'
echo "    POST ${BASE}/api/v1/problems/sync :"
curl -s -X POST "${BASE}/api/v1/problems/sync" | head -c 300; echo ""
sleep 2
curl -s "${BASE}/api/v1/problems?limit=200" | grep -q "zops-drill" || {
  echo "    [错误] 后端归档查无演练事件, 贴上面 sync 输出排查。容器已保护性拉起。"
  restore_drill; exit 1
}
curl -s "${BASE}/api/v1/problems?limit=200" | python3 -c '
import json, sys
rows = [p for p in json.load(sys.stdin)["data"]["items"] if "zops-drill" in p["trigger_name"]]
print("ARCHIVED=%d" % len(rows))
for p in rows:
    print("    eventid=%s status=%s started=%s" % (p["zabbix_eventid"], p["status"], p["started_at"]))
'
echo "    [OK] 告警已进后端归档 problem_records"

echo "==> [6/7] 拉起 ${DRILL}, 等待 [RECOVERED] 与归档翻转"
docker start "${DRILL}" >/dev/null
STOPPED=0
if ! RES=$(wait_item 1 600); then
  echo "    [错误] 容器已拉起但采集值 10 分钟未回 1。容器在运行, 重跑本脚本继续判断。"
  exit 1
fi
echo "    ${RES} [OK] 运行态已恢复采集"
if ! TL=$(wait_trigger 0 "$TID" 300); then
  echo "    [错误] 触发器 5 分钟未回 OK, recovery_expression 不匹配? 贴输出排查。"
  exit 1
fi
echo "    ${TL} [OK] Zabbix 侧已恢复 (status=0)"
curl -s -X POST "${BASE}/api/v1/problems/sync" >/dev/null
sleep 2
curl -s "${BASE}/api/v1/problems?limit=200" | python3 -c '
import json, sys
rows = [p for p in json.load(sys.stdin)["data"]["items"] if "zops-drill" in p["trigger_name"]]
ok = any(p["recovered_at"] for p in rows)
print("    归档 drill 记录 %d 条, recovered_at 已回填: %s" % (len(rows), ok))
'
echo "    Zabbix 事件流水 (PROBLEM 行 r_eventid 应指向恢复事件, 不再是 0):"
api events "$TID" | sed 's/^/    /'

echo "==> [7/7] 演练完成"
echo "    实证链路: stop→采集0→[PROBLEM]→sync归档→start→采集1→[RECOVERED]→recovered_at 回填"
echo "    请接着跑只读验收: bash scripts/verify_phase8.sh   (通关标准 PASS=5 FAIL=0)"
