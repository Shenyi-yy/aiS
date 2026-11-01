#!/usr/bin/env bash
# =============================================================
# Phase 7 验证脚本: Docker 监控 (agent2 内建检查项 + 模板 LLD)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase7.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: 已跑 setup_docker_monitoring.sh; UI 已挂 "Docker by Zabbix agent 2"
#           模板并对 Docker containers 发现规则 Execute now; 已 --build backend
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

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

echo "== [1/6] agent2 Docker 插件真实可用 (收紧版: 实测 docker.ping 应答) =="
# 旧判据 grep -qi version 是假 PASS: 错误文本 "Unknown metric docker.version"
# 自带 version 字样。7.0 实证合法键是 docker.ping(应答1)/docker.info(JSON)。
VM_IP=$(hostname -I | awk '{print $1}')
OUT=$(docker exec zops-zabbix-server zabbix_get -s "${VM_IP}" -k docker.ping 2>&1 || true)
if [ "$(echo "${OUT}" | tr -d '[:space:]')" = "1" ]; then
  ok "zabbix_get docker.ping -> 1 (经 10050 实测, agent2 Docker 插件真答)"
else
  ng "docker.ping 应答异常: ${OUT} — 先跑 setup_docker_monitoring.sh, 再查 agent2"
fi

echo "== [2/6] 宿主机 Docker 真实运行容器数 (本机 ground truth) =="
N=$(${SUDO} docker ps -q 2>/dev/null | wc -l)
if [ "${N}" -ge 4 ]; then
  ok "docker ps 运行中容器 ${N} 个: $(${SUDO} docker ps --format '{{.Names}}' | tr '\n' ' ')"
else
  ng "只看到 ${N} 个运行容器 — 基础设施没起来? docker ps 检查一下"
fi

echo "== [3/6] 后端 0.7.0 + 新路由 /api/v1/docker/summary 已注册 =="
STATE=$(docker inspect -f '{{.State.Status}}' zops-backend 2>/dev/null || echo missing)
HAS=$(curl -sf --max-time 5 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    print("yes" if "/api/v1/docker/summary" in json.load(sys.stdin)["paths"] else "no")
except Exception:
    print("no")')
VER=$(curl -sf --max-time 5 "${BASE}/health" | jget version)
if [ "${STATE}" = "running" ] && [ "${HAS}" = "yes" ]; then
  ok "zops-backend running, version=${VER}, /api/v1/docker/summary 已注册"
else
  ng "state=${STATE} 路由=${HAS} — 没重 build: docker compose up -d --build backend"
fi

echo "== [4/6] Zabbix 已发现 docker.* 监控项 (模板挂接 + LLD 生效证据) =="
SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary" || echo "")
IT=$(echo "${SUM}" | jget "data.item_total")
case "${IT}" in
  ''|*[!0-9]*) ng "summary 无有效返回 — Zabbix 登录问题先跑 verify_phase5.sh [2/6]; 详情: curl -s ${BASE}/api/v1/docker/summary" ;;
  0) ng "item_total=0: 模板没挂上或发现规则没跑 — UI: ubuntu-host 挂 'Docker by Zabbix agent 2', 再对 'Docker containers' 发现规则 Execute now, 等 1-2 分钟重跑" ;;
  *) if [ "${IT}" -ge 20 ]; then
       ok "docker.* 监控项 ${IT} 条 (agent 内建项 + LLD 发现), 真实存在非 fake"
     else
       ng "只有 ${IT} 条 docker 项 — 发现可能不完整, 贴回来人工看"
     fi ;;
esac

echo "== [5/6] 发现覆盖: 每个运行容器都有监控项 + Docker 触发器已实例化 =="
CN=$(echo "${SUM}" | jget "data.discovered_container_count")
TR=$(echo "${SUM}" | jget "data.trigger_total")
case "${CN}${TR}" in
  ''|*[!0-9]*) ng "拿不到计数 (got CN='${CN}' TR='${TR}')" ;;
  *) if [ "${CN}" -ge "${N}" ] && [ "${TR}" -ge 1 ]; then
       ok "LLD 发现容器 ${CN} 台 (>= 运行中 ${N}) · Docker 触发器 ${TR} 个已实例化"
     else
       ng "发现 ${CN} < 运行 ${N} 或触发器 ${TR}=0 — LLD 未跑完? 等 1-2 分钟重跑本脚本"
     fi ;;
esac

echo "== [6/6] 收数健康: docker 项无 unsupported / error =="
UP=$(echo "${SUM}" | jget "data.item_unsupported")
ER=$(echo "${SUM}" | jget "data.item_with_error")
case "${UP}${ER}" in
  ''|*[!0-9]*) ng "拿不到健康计数 (UP='${UP}' ER='${ER}')" ;;
  *) if [ "${UP}" = "0" ] && [ "${ER}" = "0" ]; then
       ok "全部 docker 监控项 state=0 且无 error — 数据真实在流动"
     else
       ng "unsupported=${UP} error=${ER} — 大概率 agent 权限/模板宏问题, 明细见 curl -s '${BASE}/api/v1/docker/summary'"
     fi ;;
esac

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 7 验证通过, 可以进入 Phase 8"
  echo "浏览器看 http://<Ubuntu_IP>:8000/ 左列新增 'Docker 容器监控' 面板 (绿点=正常收数)"
else
  echo "Phase 7 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
