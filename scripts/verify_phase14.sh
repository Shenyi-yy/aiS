#!/usr/bin/env bash
# =============================================================
# Phase 14 验证脚本: 审计日志 (sha256 哈希链, 只追加, 全链时间线)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase14.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: Phase 13 已通关; 已 scp 本阶段代码 + up -d --build backend;
#           zops-drill 容器存在; .env 未关 AUDIT_ENABLED (缺省即开)
# 关键门禁: [4/6] 闭环每一步 (自动分析/建议/批准) 都必须已入链, 且批准前
#           容器仍是 exited —— 审计不是事后补写, 是当场留痕;
#           [6/6] 全链重放校验 ok=true —— 任何一环缺签名/断链都过不了。
# 防陈旧: [4/6] 只认 id>基线 的本轮新归档 (首轮教训: 无审计时代留下的
#         ACTIVE 老问题会被误当本轮目标, 它的分析事件永远等不到)。
# 破坏性: 会 stop/start zops-drill (不碰核心 5 容器); trap 保证拉回。
#         本脚本【不】演示篡改检测 —— 改真库会让链永久断; 篡改场景由
#         tests/test_audit.py 在隔离库覆盖, 现场只看重放校验是否 ok。
# 说明: [0] 自带 /health 就绪等待; [6/6] 沿用教训 —— drill 拉起后轮询
#       容器系 unsupported 归零, 镜像系模板噪音单列不卡闸。全程 4-10 分钟。
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

DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then DOCKER="sudo docker"; fi

cleanup() {
  echo "  (收尾: 确保 zops-drill 已拉起)"
  $DOCKER start zops-drill >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== [0] 等待后端就绪 (防抢跑) =="
READY=no
for i in $(seq 1 30); do
  if curl -sf --max-time 3 "${BASE}/health" >/dev/null 2>&1; then READY=yes; break; fi
  sleep 3
done
if [ "${READY}" != "yes" ]; then
  echo "  后端 90s 未就绪, 后续检查必然全挂 —— 看 docker logs zops-backend"
fi

echo "== [1/6] backend 0.14.0 + 审计三件套已入镜像 + 路由注册 =="
VER=$(curl -s --max-time 10 "${BASE}/openapi.json" | jget info.version)
INIMG=$(docker exec zops-backend sh -c 'for f in app/services/audit.py app/schemas/audit.py app/api/routes/audit.py; do test -f /app/$f || exit 1; done; echo yes' 2>/dev/null || echo no)
HASROUTE=$(curl -s --max-time 10 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
except Exception:
    print("no"); raise SystemExit
need={"/api/v1/audit","/api/v1/audit/verify","/api/v1/audit/timeline/problem/{problem_id}"}
print("yes" if need <= set(p) else "no")')
if [ "${VER}" = "0.14.0" ] && [ "${INIMG}" = "yes" ] && [ "${HASROUTE}" = "yes" ]; then
  ok "版本 ${VER}, 服务/模型/路由三件套在容器内, /audit 三个 GET 路由已注册"
else
  ng "version=${VER:-不可达} files=${INIMG} routes=${HASROUTE} —— scp 后必须 up -d --build backend"
fi

echo "== [2/6] 只追加结构证据: /audit 面只有 GET, 一个写方法都不许有 =="
GETONLY=$(curl -s --max-time 10 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
except Exception:
    print("bad"); raise SystemExit
w=0
for path,methods in p.items():
    if path.startswith("/api/v1/audit"):
        w+=len(set(methods)-{"get"})
print("clean" if w==0 else "dirty:%d" % w)')
LST=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${BASE}/api/v1/audit?limit=5")
VFY=$(curl -s --max-time 30 "${BASE}/api/v1/audit/verify")
VOK=$(echo "${VFY}" | jget data.ok)
if [ "${GETONLY}" = "clean" ] && [ "${LST}" = "200" ] && [ "${VOK}" = "True" ]; then
  ok "审计面零写方法 (openapi 可逐字核对); 列表 200; 现有链重放校验 ok (checked=$(echo "${VFY}" | jget data.checked))"
else
  ng "methods=${GETONLY} list=${LST} verify_ok=${VOK:-?} reason=$(echo "${VFY}" | jget data.reason)"
fi

echo "== [3/6] system.startup 事件: 进程自己也是被审计对象 =="
SU=$(curl -s --max-time 10 "${BASE}/api/v1/audit?event_type=system.startup&limit=5")
SUTOT=$(echo "${SU}" | jget data.total)
SUVER=$(echo "${SU}" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
    print(its[0]["detail"].get("version","") if its else "")
except Exception:
    print("")')
if [ "${SUTOT:-0}" -ge 1 ] 2>/dev/null && [ "${SUVER}" = "0.14.0" ]; then
  ok "启动事件已入链 ${SUTOT} 条, 最新 detail.version=${SUVER} (开关状态全在 detail 里)"
else
  ng "total=${SUTOT:-0} version=${SUVER:-不可读} —— 期望 >=1 且 0.14.0; 若后端刚重建等首轮归档后重跑"
fi

echo "== [4/6] 硬门禁: 演练闭环自动分析+建议已入链, 但无人批准 → 容器必须还停着 =="
# 首轮 VM 教训: 上一代(无审计)进程留下的 ACTIVE 老问题会被这里当成
# "本轮新问题" —— 它永远不会再有新分析事件, 干等 600s。对策: 先取
# 归档 id 基线, stop 之后只认 **id 大于基线** 的新归档。
BASE_MAX=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
except Exception:
    items=[]
print(max([it["id"] for it in items], default=0))')
$DOCKER stop zops-drill >/dev/null
echo "    zops-drill 已停止 (基线: 现有归档最大 id=${BASE_MAX}); 等归档 (<=320s) → AUTO 分析+建议 (<=600s)..."
PID=""
for i in $(seq 1 40); do
  PID=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
except Exception:
    items=[]
act=[it for it in items if it["status"]=="ACTIVE" and "zops-drill" in it["trigger_name"] and it["id"]>int(sys.argv[1])]
print(act[0]["id"] if act else "")' "${BASE_MAX}")
  [ -n "${PID}" ] && break
  sleep 8
done
EVOK=""
if [ -z "${PID}" ]; then
  ng "320s 内没归档到 id>${BASE_MAX:-?} 的新 ACTIVE zops-drill 问题 —— 若有老单挡着先拉起 drill 等其恢复; 否则查触发器 25333 与归档循环日志"
else
  for i in $(seq 1 40); do
    EVOK=$(curl -s --max-time 15 "${BASE}/api/v1/audit?link_problem_id=${PID}" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
t={x["event_type"] for x in its}
need={"analysis.success","execution.request_created"}
src=[x for x in its if x["event_type"]=="analysis.success" and x["source"]=="AUTO"]
print("ok" if need <= t and src else "")')
    [ -n "${EVOK}" ] && break
    sleep 15
  done
fi
if [ "${EVOK}" != "ok" ]; then
  ng "问题 ${PID:-?} 600s 内审计链没等到 AUTO 分析+建议事件 —— 看 backend 日志 (P12/P13 链路?)"
else
  STATE=$(docker inspect -f '{{.State.Status}}' zops-drill 2>/dev/null || echo 查询失败)
  if [ "${STATE}" = "exited" ]; then
    ok "问题 ${PID}: analysis.success(AUTO)+execution.request_created 均已当场入链, 且 zops-drill 仍是 exited —— 审计先行, 执行等人类"
  else
    ng "容器状态=${STATE} —— 建议未批准就被执行了, 红线, 立刻排查"
  fi
fi

echo "== [5/6] 人工批准 → execution.approved 入链 (actor=审批人) → 告警自动 RECOVERED =="
REQ=$(curl -s --max-time 10 "${BASE}/api/v1/exec/requests?problem_record_id=${PID:-0}&status=PENDING_APPROVAL" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
auto=[x for x in its if x["source"]=="AUTO"]
print(auto[0]["id"] if auto else "")')
if [ -z "${REQ:-}" ]; then
  ng "没找到问题 ${PID:-?} 的待审批单 —— [4/6] 链路没走全"
else
  ACODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "${BASE}/api/v1/exec/requests/${REQ}/approve" \
    -H 'Content-Type: application/json' \
    -d '{"approver":"yangshiyi","comment":"同意: P14 审计闭环演练"}')
  AEV=""
  for i in $(seq 1 6); do
    AEV=$(curl -s --max-time 15 "${BASE}/api/v1/audit?event_type=execution.approved&link_problem_id=${PID}" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
hit=[x for x in its if x["actor"]=="yangshiyi"]
print("ok" if hit else "")')
    [ -n "${AEV}" ] && break
    sleep 5
  done
  REC=no
  ST=""
  if [ "${ACODE}" = "200" ] && [ "${AEV}" = "ok" ]; then
    echo "    批准 ${REQ} 已执行并入链; 等问题 ${PID} 转 RECOVERED (<=300s)..."
    for i in $(seq 1 20); do
      ST=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
except Exception:
    items=[]
m=[it["status"] for it in items if it["id"]==int(sys.argv[1])]
print(m[0] if m else "")' "${PID}")
      if [ "${ST}" = "RECOVERED" ]; then REC=yes; break; fi
      sleep 15
    done
  fi
  if [ "${REC}" = "yes" ]; then
    ok "批准→执行→execution.approved(actor=yangshiyi) 入链→问题 ${PID} RECOVERED, 审计与事实同步"
  else
    ng "approve=${ACODE} audit_event=${AEV:-无} 问题状态=${ST:-?} —— 期望 200/ok/RECOVERED"
  fi
fi

echo "== [6/6] 全链重放 + 时间线组装 + tick 持久化 + 数据面回归 =="
VFY2=$(curl -s --max-time 30 "${BASE}/api/v1/audit/verify")
VOK2=$(echo "${VFY2}" | jget data.ok)
TL=$(curl -s --max-time 15 "${BASE}/api/v1/audit/timeline/problem/${PID:-0}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]
except Exception:
    print("bad"); raise SystemExit
ev={x["event_type"] for x in d["events"]}
need={"analysis.success","execution.request_created","execution.approved"}
ok= d["problem"] and len(d["analyses"])>=1 and len(d["executions"])>=1 and need<=ev
print("ok" if ok else "bad:%s" % sorted(ev))' | tr -d " ")
TICK=$(curl -s --max-time 10 "${BASE}/api/v1/audit?event_type=dispatcher.tick&limit=5" | jget data.total)
SUP="x"; IMG="?"
for i in $(seq 1 8); do
  SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary")
  S=$(echo "${SUM}" | jget data.item_unsupported)
  IMG=$(echo "${SUM}" | jget data.item_unsupported_image)
  if [ "${S:-x}" = "0" ]; then SUP=0; break; fi
  SUP="${S:-x}"
  sleep 15
done
if [ "${VOK2}" = "True" ] && [ "${TL}" = "ok" ] && [ "${TICK:-0}" -ge 1 ] 2>/dev/null && [ "${SUP}" = "0" ]; then
  ok "重放校验 ok (checked=$(echo "${VFY2}" | jget data.checked)); 时间线四要素齐 (问题+分析+单据+事件); dispatcher.tick 已持久化 ${TICK} 轮; 容器系 unsupported=0 (镜像系噪音=${IMG} 单列不卡闸)"
else
  ng "verify=${VOK2:-?} timeline=${TL} tick=${TICK:-0} unsupported(容器系)=${SUP} image=${IMG} —— 期望 True/ok/>=1/0"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 14 验证通过, 可以进入 Phase 15 (RAG 知识库)"
  echo "复盘: 启动/分析/建议/批准/否决/调度轮次全部入链, /audit 只读可重放"
else
  echo "Phase 14 未通过: 按上面 [FAIL] 行排查后重跑本脚本"
fi
