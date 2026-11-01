#!/usr/bin/env bash
# =============================================================
# Phase 6 验证脚本: Web Dashboard (监控大盘) + 主机接口
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase6.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: Phase 5 已通关; 本阶段新增 backend/app/static 与 /api/v1/hosts,
#           必须先 docker compose up -d --build backend
# 判读提示: 每项自己打印 [PASS]/[FAIL]; 把完整输出贴回来即可。
# =============================================================
set -u

BASE="http://127.0.0.1:${BACKEND_PORT:-8000}"
PASS=0
FAIL=0

ok() { echo "  [PASS] $1"; PASS=$((PASS+1)); }
ng() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

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

echo "== [1/6] 大盘首页 GET / 返回 HTML =="
CODE=$(curl -s -o /tmp/zops_index.html -w '%{http_code}' --max-time 10 "${BASE}/")
TITLE=$(grep -o "ZOPS 监控大盘" /tmp/zops_index.html 2>/dev/null | head -1)
CT=$(curl -s -o /dev/null -w '%{content_type}' --max-time 10 "${BASE}/")
if [ "${CODE}" = "200" ] && [ -n "${TITLE}" ]; then
  ok "HTTP ${CODE} + content_type=${CT} + 标题命中 '${TITLE}'"
else
  ng "HTTP ${CODE} 标题='${TITLE:-空}' (非 200 多半是没 --build 重跑; 404=static 目录没进去镜像)"
fi

echo "== [2/6] /health 未被静态挂载吞掉 (响应形状与 Phase 1 一致) =="
H=$(curl -sf --max-time 10 "${BASE}/health")
HS=$(echo "${H}" | jget "status")
HSV=$(echo "${H}" | jget "service")
if [ "${HS}" = "ok" ] && [ "${HSV}" = "zops-backend" ]; then
  ok "GET /health 仍是 JSON 存活检查 (StaticFiles 注册在路由之后, 顺序正确)"
else
  ng "/health 返回异常 (status='${HS}' service='${HSV}') — 静态挂载抢先匹配了"
fi

echo "== [3/6] 新接口 GET /api/v1/hosts 返回真实主机 =="
HOSTS=$(curl -sf --max-time 20 "${BASE}/api/v1/hosts" || echo "")
HN=$(echo "${HOSTS}" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]
    names=[h["technical_name"] for h in items]
    print(str(len(items)) + "|" + ",".join(names))
except Exception:
    print("err|")')
HCNT=${HN%%|*}
HLIST=${HN#*|}
case "${HCNT}" in
  ''|*[!0-9]*) ng "hosts 接口无有效返回 (可能是 Zabbix 登录失败, 先跑 Phase 5 的 [2/6] 确认)" ;;
  *) if [ "${HCNT}" -ge 2 ] && echo "${HLIST}" | grep -q "ubuntu-host"; then
       ok "受监控主机 ${HCNT} 台: ${HLIST} (host.get status=0, 真实 API)"
     else
       ng "只拿到 ${HCNT} 台: ${HLIST} — 期望含 ubuntu-host + zabbix-agent"
     fi ;;
esac

echo "== [4/6] 大盘数据源 problems 仍有真实归档 =="
PROB=$(curl -sf --max-time 10 "${BASE}/api/v1/problems?limit=100")
T=$(echo "${PROB}" | jget "data.total")
ZC=$(echo "${PROB}" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
    print(len([i for i in items if str(i["trigger_name"]).startswith("ZOPS:")]))
except Exception:
    print("")')
case "${T}" in
  ''|*[!0-9]*) ng "problems 接口无有效返回 (total='${T}')" ;;
  *) if [ "${T}" -ge 1 ] && [ "${ZC:-0}" -ge 1 ] 2>/dev/null; then
       ok "total=${T}, 其中 ZOPS 归档 ${ZC} 条 (Phase 5 数据仍在)"
     else
       ng "total=${T} ZOPS=${ZC:-空} — 归档丢了? 先 curl -X POST ${BASE}/api/v1/problems/sync?days=30"
     fi ;;
esac

echo "== [5/6] 零假数据: 大盘 JS 只引用真实接口 =="
MISS=""
for ep in /api/v1/system/info /api/v1/hosts /api/v1/problems /api/v1/problems/sync; do
  grep -q "${ep}" /tmp/zops_index.html || MISS="${MISS} ${ep}"
done
# 大盘横幅里"零假数据"是自述, 不算命中; 其余出现 mock/dummy 等字样才是真造假
FAKE=$(grep -nE "mock|faker|dummy|示例数据|假数据" /tmp/zops_index.html | grep -vc "零假数据" || true)
if [ -z "${MISS}" ] && [ "${FAKE:-0}" = "0" ]; then
  ok "四个真实接口全部被引用, 且全文无 mock/dummy 字样"
else
  ng "缺引用:${MISS:-无} mock 命中=${FAKE:-?} — 贴回来人工看"
fi

echo "== [6/6] 同步按钮等价调用: POST /sync 后 total 稳定 (幂等) =="
curl -sf --max-time 30 -X POST "${BASE}/api/v1/problems/sync?days=30" >/dev/null || true
T2=$(curl -sf --max-time 10 "${BASE}/api/v1/problems?limit=5" | jget "data.total")
if [ -n "${T}" ] && [ "${T2}" = "${T}" ]; then
  ok "同步后 total 不变 (${T} = ${T2}), 前端刷新按钮可放心点"
else
  ng "total 从 ${T} 变成 ${T2} — 贴回来排查"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 6 验证通过, 可以进入 Phase 7"
  echo "浏览器打开: http://<Ubuntu_IP>:8000/  (账号 Admin, 大盘无需登录)"
else
  echo "Phase 6 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
