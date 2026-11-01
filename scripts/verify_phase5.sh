#!/usr/bin/env bash
# =============================================================
# Phase 5 验证脚本: Zabbix API 接入 + 问题归档
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase5.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: /opt/zops/.env 已填 ZABBIX_USERNAME/ZABBIX_PASSWORD (Web 登录密码)
#           且 docker compose up -d --build backend 已执行
# 判读提示: 每项自己打印 [PASS]/[FAIL]; 把完整输出贴回来即可。
# =============================================================
set -u

BASE="http://127.0.0.1:${BACKEND_PORT:-8000}"
PASS=0
FAIL=0

ok() { echo "  [PASS] $1"; PASS=$((PASS+1)); }
ng() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

# 从 stdin 的 JSON 里按 "a.b.0.c" 路径取值 (失败打印空串)
jget() {
  python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        d=d[int(k)] if k.isdigit() else d[k]
    print(d)
except Exception:
    print("")' "$1"
}

echo "== [1/6] backend 容器运行 + Phase 5 新路由已注册 =="
STATE=$(docker inspect -f '{{.State.Status}}' zops-backend 2>/dev/null || echo missing)
HAS_ROUTE=$(curl -sf --max-time 5 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    print("yes" if "/api/v1/zabbix/health" in json.load(sys.stdin)["paths"] else "no")
except Exception:
    print("no")')
if [ "${STATE}" = "running" ] && [ "${HAS_ROUTE}" = "yes" ]; then
  ok "zops-backend running 且 /api/v1/zabbix/health 已注册"
else
  ng "state=${STATE} 新路由=${HAS_ROUTE} (路由 no=还在跑旧镜像: docker compose up -d --build backend)"
fi

echo "== [2/6] Zabbix API 登录自检 GET /api/v1/zabbix/health =="
VER=$(curl -sf --max-time 10 "${BASE}/api/v1/zabbix/health" | jget "data.api_version")
case "${VER}" in
  7.*) ok "登录成功, apiinfo.version=${VER} (真实 API 调用)" ;;
  *)   ng "拿不到版本号 (got='${VER}'); 大概率 .env 密码不对, 排查:"
       echo "        1) 看错误详情: curl -s ${BASE}/api/v1/zabbix/health ; docker logs zops-backend --tail 20"
       echo "        2) 核对容器里的值: docker exec zops-backend env | grep ZABBIX_ (PASSWORD 应非空)"
       echo "        3) 改过 /opt/zops/.env 后必须: docker compose up -d backend" ;;
esac

echo "== [3/6] 首次归档同步 POST /api/v1/problems/sync?days=30 =="
SYNC=$(curl -sf --max-time 30 -X POST "${BASE}/api/v1/problems/sync?days=30" || echo "")
F=$(echo "${SYNC}" | jget "data.fetched")
I=$(echo "${SYNC}" | jget "data.inserted")
case "${F}" in
  ''|*[!0-9]*) ng "sync 接口无有效返回 (fetched='${F}'); curl -v 复测: curl -s -X POST ${BASE}/api/v1/problems/sync" ;;
  *) if [ "${F}" -ge 1 ]; then
       ok "拉到 ${F} 条真实事件, 新增归档 ${I} 条 (Phase 3 压测产生的 ZOPS 事件)"
     else
       ng "fetched=0: Zabbix 近 30 天没有 PROBLEM 事件? 跑一遍 bash scripts/trigger_drill.sh 240 再试"
     fi ;;
esac

echo "== [4/6] 归档查询 GET /api/v1/problems 真实数据校验 =="
PROB=$(curl -sf --max-time 10 "${BASE}/api/v1/problems?limit=100")
T=$(echo "${PROB}" | jget "data.total")
# 归档是全量的: 自建 ZOPS 触发器 + 模板自带触发器都会进来, 只要求存在 ZOPS 事件即可
ZINFO=$(echo "${PROB}" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
    z=[it for it in items if str(it["trigger_name"]).startswith("ZOPS:")]
    if z:
        print(str(len(z)) + "|" + str(z[0]["trigger_name"]) + "|" + str(z[0]["status"]))
    else:
        print("0|none")
except Exception:
    print("err|")')
ZCNT=${ZINFO%%|*}
ZFIRST=${ZINFO#*|}
case "${T}" in
  ''|*[!0-9]*) ng "problems 接口无有效返回 (total='${T}')" ;;
  *) if [ "${T}" -ge 1 ] && [ "${ZCNT}" -ge 1 ] 2>/dev/null; then
       ok "total=${T} (含模板真实事件), 其中 ZOPS 归档 ${ZCNT} 条: ${ZFIRST}"
     else
       ng "total=${T} 但没找到 ZOPS 触发器归档 (ZOPS数='${ZCNT}') — 贴回来人工看"
     fi ;;
esac

echo "== [5/6] 幂等去重: 再同步一次, total 不应变化 =="
curl -sf --max-time 30 -X POST "${BASE}/api/v1/problems/sync?days=30" >/dev/null || true
T2=$(curl -sf --max-time 10 "${BASE}/api/v1/problems?limit=5" | jget "data.total")
if [ -n "${T}" ] && [ "${T2}" = "${T}" ]; then
  ok "两次同步后 total 稳定 (${T} = ${T2}), zabbix_eventid 唯一约束生效"
else
  ng "total 从 ${T} 变成 ${T2} — 重复入库了, 贴回来排查"
fi

echo "== [6/6] 数据真实性交叉核对: 归档的 eventid 存在于 Zabbix PostgreSQL =="
EID=$(curl -sf --max-time 10 "${BASE}/api/v1/problems?limit=1" | jget "data.items.0.zabbix_eventid")
case "${EID}" in
  ''|*[!0-9]*) ng "拿不到数字 eventid (got='${EID}')" ;;
  *) CNT=$(docker exec zops-postgres psql -U zabbix -d zabbix -tAc "select count(*) from events where eventid = ${EID};" 2>/dev/null || echo err)
     if [ "${CNT}" = "1" ]; then
       ok "eventid=${EID} 在 Zabbix events 表实至名归 → 归档的是真实告警, 非 fake"
     else
       ng "eventid=${EID} 在 Zabbix events 表查不到 (got='${CNT}') — 数据来源可疑"
     fi ;;
esac

echo
echo "== 附: Phase 3 磁盘 item 遗留诊断 (不计分, 把这段输出一起贴回来) =="
curl -s --max-time 10 "${BASE}/api/v1/zabbix/items?host_name=ubuntu-host&key=vfs.fs.size%5B%2F%2Cpused%5D" | python3 -m json.tool 2>/dev/null || echo "  (items 诊断接口无返回)"

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 5 验证通过, 可以进入 Phase 6"
else
  echo "Phase 5 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
