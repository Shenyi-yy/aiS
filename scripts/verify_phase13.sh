#!/usr/bin/env bash
# =============================================================
# Phase 13 验证脚本: 人工确认 + 安全执行 (建议→审批→白名单执行→留痕)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase13.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: Phase 12 已通关; 已 scp 本阶段代码 + --build backend +
#           docker compose -p zops up -d backend (注入 EXEC_* 与 socket 挂载);
#           zops-drill 容器存在
# 关键门禁: [3/6] 未批准前零执行 —— 后台自动分析并生成了"重启 zops-drill"
#           的待审批建议后, 容器必须**仍然是停止状态** (没有任何代码替人
#           做主); [4/6] 人工批准后才真正重启且告警自动恢复。
# 破坏性: 本脚本会 stop/start zops-drill 演练容器 (只碰它, 不碰核心 5 容器),
#         trap 保证异常退出也会把 drill 拉回来。
# 说明: 脚本开头自带 /health 就绪等待; [6/6] 沿用 P12 教训 —— 检查 docker
#       数据面前确保 drill 已拉起并等 unsupported 归零, 防停止期假阳性。
#       全程预计 4-10 分钟 (归档 + 自动分析 40-90s + 恢复检测各一段)。
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

echo "== [1/6] backend 0.13.0 + 执行层三件套已入镜像 =="
VER=$(curl -s --max-time 10 "${BASE}/openapi.json" | jget info.version)
INIMG=$(docker exec zops-backend sh -c 'for f in app/services/exec_catalog.py app/services/docker_exec.py app/services/execution.py; do test -f /app/$f || exit 1; done; echo yes' 2>/dev/null || echo no)
HASROUTE=$(curl -s --max-time 10 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
except Exception:
    print("no"); raise SystemExit
print("yes" if "/api/v1/exec/requests/{request_id}/approve" in p and "/api/v1/exec/operations" in p else "no")')
if [ "${VER}" = "0.13.0" ] && [ "${INIMG}" = "yes" ] && [ "${HASROUTE}" = "yes" ]; then
  ok "版本 ${VER}, 目录/执行器/状态机三件套在容器内, /exec 路由已注册"
else
  ng "version=${VER:-不可达} files=${INIMG} route=${HASROUTE} —— scp 后必须 --build backend 且 up -d backend"
fi

echo "== [2/6] 白名单结构证据: 目录/开关/socket 全诚实 =="
CFG=$(curl -s --max-time 10 "${BASE}/api/v1/exec/config")
OPS=$(curl -s --max-time 10 "${BASE}/api/v1/exec/operations")
W2=$(printf '%s\n%s\n' "${CFG}" "${OPS}" | python3 -c 'import json,sys
lines=sys.stdin.read().split("\n")
cfg=json.loads(lines[0])["data"]; ops=json.loads(lines[1])["data"]
core={"zops-postgres","zops-zabbix-server","zops-zabbix-web","zops-zabbix-agent","zops-backend"}
errs=[]
if not cfg["enabled"]: errs.append("EXEC_ENABLED=false")
if not cfg["socket_exists"]: errs.append("socket 未挂进容器(compose 缺 volumes 行?)")
if cfg["approval_required"] is not True: errs.append("approval_required!=True")
if set(cfg["allow_targets"]) != {"zops-drill"}: errs.append("allow_targets=%s" % cfg["allow_targets"])
if sorted(ops["allowed_actions"]) != ["restart","start","stop"]: errs.append("actions=%s" % ops["allowed_actions"])
for op in ops["operations"]:
    if op["target"] not in cfg["allow_targets"]: errs.append("op %s 目标越白名单" % op["op_id"])
    if op["target"] in core: errs.append("op %s 指向核心容器!" % op["op_id"])
print("ok ops=%d pending=%s" % (len(ops["operations"]), cfg["pending_requests"]) if not errs else "; ".join(errs))')
# 生死探针: 首跑教训 —— 镜像里是"文件存在"(socket_exists=true)但容器内
# 非 root 的 zops 用户未必"连得上去" (docker.sock 属 root:docker)。
# 用与 backend 完全相同的身份试 connect, 权限问题在这里就拦下, 不留到 [4/6]。
SOCKPROBE=$(docker exec -i zops-backend python3 - <<'PY' 2>&1
import socket
s = socket.socket(socket.AF_UNIX)
try:
    s.connect("/var/run/docker.sock")
    print("ok")
except OSError as e:
    print("denied:%s" % e)
finally:
    s.close()
PY
)
if [ "${W2#ok}" = "${W2}" ]; then
  ng "${W2}"
elif [ "${SOCKPROBE}" != "ok" ]; then
  ng "容器内 zops 用户连不上 docker.sock: ${SOCKPROBE} —— .env 补 DOCKER_GID=\$(stat -c '%g' /var/run/docker.sock) 后 up -d backend"
else
  ok "${W2}; 核心 5 容器不在目标白名单, 动作只有固定三件套, 审批恒必需, 容器内实连 socket 通过"
fi

echo "== [3/6] 硬门禁: 自动建议已生成, 但无人批准 → 容器必须还停着 (零擅自执行) =="
$DOCKER stop zops-drill >/dev/null
echo "    zops-drill 已停止; 等归档 (≤320s) → 自动分析 (≤480s) → 待审批建议 (≤180s)..."
PID=""
for i in $(seq 1 40); do
  PID=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
except Exception:
    items=[]
act=[it for it in items if it["status"]=="ACTIVE" and "zops-drill" in it["trigger_name"]]
print(act[0]["id"] if act else "")')
  [ -n "${PID}" ] && break
  sleep 8
done
REQ=""
if [ -z "${PID}" ]; then
  ng "320s 内没归档到 ACTIVE 的 zops-drill 问题 —— 检查触发器 25333 与归档循环日志"
else
  echo "    新问题 id=${PID}; 等 trigger_source=AUTO 的 SUCCESS 分析..."
  ANA=""
  for i in $(seq 1 32); do
    A=$(curl -s --max-time 15 "${BASE}/api/v1/ai/analyses?problem_record_id=${PID}&status=SUCCESS&limit=5" | jget data.total)
    if [ "${A:-0}" -ge 1 ] 2>/dev/null; then ANA=1; break; fi
    sleep 15
  done
  if [ -z "${ANA}" ]; then
    ng "问题 ${PID} 480s 内没出 AUTO SUCCESS 分析 (P12 链路?) —— 建议无从生成"
  else
    for i in $(seq 1 12); do
      REQ=$(curl -s --max-time 10 "${BASE}/api/v1/exec/requests?problem_record_id=${PID}&status=PENDING_APPROVAL" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
auto=[x for x in its if x["source"]=="AUTO" and x["op_id"]=="restart_zops_drill"]
print(auto[0]["id"] if auto else "")')
      [ -n "${REQ}" ] && break
      sleep 15
    done
  fi
fi
if [ -z "${REQ:-}" ]; then
  ng "分析成功但 180s 内没生成待审批建议 —— 看 backend 日志 '已生成执行建议'; 或 EXEC_AUTO_SUGGEST 被关"
else
  STATE=$(docker inspect -f '{{.State.Status}}' zops-drill 2>/dev/null || echo 查询失败)
  RSTAT=$(curl -s --max-time 10 "${BASE}/api/v1/exec/requests/${REQ}" | jget data.status)
  if [ "${STATE}" = "exited" ] && [ "${RSTAT}" = "PENDING_APPROVAL" ]; then
    ok "建议单 ${REQ} 停在 PENDING_APPROVAL 且 zops-drill 仍是 exited —— AI 只能建议, 执行必须等人"
  else
    ng "单据 ${REQ} 状态=${RSTAT}, 容器状态=${STATE} —— 未批准就执行是红线, 立刻排查"
  fi
fi

echo "== [4/6] 人工批准 → 白名单执行 → 容器恢复 → 告警自动 RECOVERED =="
if [ -n "${REQ:-}" ] && [ "${RSTAT:-}" = "PENDING_APPROVAL" ]; then
  APPR=$(curl -s --max-time 60 -w '\n%{http_code}' -X POST "${BASE}/api/v1/exec/requests/${REQ}/approve" \
    -H 'Content-Type: application/json' \
    -d '{"approver":"yangshiyi","comment":"同意: 按 AI 分析建议重启演练容器"}')
  ACODE=$(echo "${APPR}" | tail -1)
  ABODY=$(echo "${APPR}" | sed '$d')
  AST=$(echo "${ABODY}" | jget data.status)
  if [ "${ACODE}" = "200" ] && [ "${AST}" = "EXECUTED" ]; then
    ok "批准生效: 单据 ${REQ} → EXECUTED ($(echo "${ABODY}" | jget data.result))"
    UP=no
    for i in $(seq 1 10); do
      [ "$(docker inspect -f '{{.State.Status}}' zops-drill 2>/dev/null)" = "running" ] && UP=yes && break
      sleep 3
    done
    if [ "${UP}" = "yes" ]; then
      echo "    zops-drill 已 running; 等归档转 RECOVERED (≤300s)..."
      REC=no
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
      if [ "${REC}" = "yes" ]; then
        ok "闭环完成: 告警→分析→建议→批准→执行→恢复, 问题 ${PID} 已 RECOVERED"
      else
        ng "容器已重启但问题 ${PID} 300s 未转 RECOVERED (当前=${ST:-?}) —— 查归档循环/Zabbix 触发器恢复"
      fi
    else
      ng "批准后 30s 内容器没起回来 —— docker logs zops-drill; 单据里 result/error 有引擎原文"
    fi
  else
    ng "批准返回 HTTP ${ACODE} status=${AST:-?}: $(echo "${ABODY}" | jget detail)"
  fi
else
  ng "[3/6] 没拿到可批准的单据, 本项无从验证"
fi

echo "== [5/6] 否决路径: reject 是终态且零执行, 批准过的单不可再动 =="
R5OUT=$(curl -s --max-time 10 -X POST "${BASE}/api/v1/exec/requests" \
  -H 'Content-Type: application/json' \
  -d '{"op_id":"restart_zops_drill","comment":"演练: 测试否决路径"}')
RID=$(echo "${R5OUT}" | jget data.id)
RJ=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "${BASE}/api/v1/exec/requests/${RID:-0}/reject" \
  -H 'Content-Type: application/json' -d '{"approver":"yangshiyi","reason":"拒绝演练: 不该重启"}')
AJ=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "${BASE}/api/v1/exec/requests/${RID:-0}/approve" \
  -H 'Content-Type: application/json' -d '{"approver":"yangshiyi"}')
ST5=$(docker inspect -f '{{.State.Status}}' zops-drill 2>/dev/null || echo 查询失败)
if [ "${RJ}" = "200" ] && [ "${AJ}" = "409" ] && [ "${ST5}" = "running" ]; then
  ok "单据 ${RID} 被拒后 REJECTED→再批准 409; 容器全程 running 未被碰过 (否决被尊重)"
else
  ng "reject=${RJ} approve=${AJ} state=${ST5} —— 期望 200/409/running"
fi

echo "== [6/6] 闸门不放松: AI 侧零执行入口 + 老口径保持 =="
# drill 在 [4/6] 已被批准重启, 但保险起见仍按 P12 教训轮询等 unsupported 归零。
# 口径 (P13 三轮): item_unsupported 只数容器系真实项 —— 每次 --build 都会让
# LLD 给新镜像 sha 新生 2 个必失败的 docker.image.created/size 模板噪音,
# 它们单列在 item_unsupported_image, 如实打印但不卡闸门。
SUP="x"; IMG="?"
for i in $(seq 1 8); do
  SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary")
  S=$(echo "${SUM}" | jget data.item_unsupported)
  IMG=$(echo "${SUM}" | jget data.item_unsupported_image)
  if [ "${S:-x}" = "0" ]; then SUP=0; break; fi
  SUP="${S:-x}"
  sleep 15
done
Z1=$(curl -s --max-time 10 "${BASE}/api/v1/ai/tools" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]
    names=[t["function"]["name"] if "function" in t else t["name"] for t in d["tools"]]
    has_exec=any(any(k in n for k in ("exec","run","restart","stop","start","shell")) for n in names)
    print("clean" if len(d["tools"])==8 and all(t["readonly"] for t in d["tools"]) and not has_exec else "bad")
except Exception:
    print("bad")')
B404=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "${BASE}/api/v1/ai/agent/99999999")
if [ "${Z1}" = "clean" ] && [ "${B404}" = "404" ] && [ "${SUP}" = "0" ]; then
  ok "AI 工具白名单 8 项恒只读且无任何执行动词; 不存在归档 404; 容器系 unsupported=0 (镜像系模板噪音=${IMG}, 单列不卡闸)"
else
  ng "tools=${Z1} agent404=${B404} unsupported(容器系)=${SUP} image_noise=${IMG} —— 容器系出现非镜像系 unsupported 才是红线"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 13 验证通过, 可以进入 Phase 14 (审计日志)"
  echo "复盘: 告警→自动分析→目录建议→人工批准/否决→白名单执行→恢复, 单据全程留痕"
else
  echo "Phase 13 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
