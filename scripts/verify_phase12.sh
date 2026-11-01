#!/usr/bin/env bash
# =============================================================
# Phase 12 验证脚本: AI 自动故障分析 (后台调度 + trigger_source 证据链)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase12.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: Phase 11 已通关; 已 scp 本阶段代码 + --build backend;
#           .env 的 LLM 三要素可用 (Phase 9 现场); zops-drill 容器存在
# 关键门禁: [4/6] 真实闭环 —— 人工 stop zops-drill 后, **没有任何 API 调用**,
#           后台必须自动完成 "归档→取证→分析" 并落 trigger_source=AUTO 记录。
# 破坏性: 本脚本会 stop/start zops-drill 演练容器 (只碰它, 不碰核心 5 容器),
#           trap 保证异常退出也会把 drill 拉回来。
# 说明: 脚本开头自带 /health 就绪等待, 根治"后端没起来就开跑"的抢跑误报。
#       全程预计 2-6 分钟 (归档 60s + 调度 60s + 模型 40-90s + 复核 75s)。
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

echo "== [1/6] backend 0.12.0 + ai_dispatcher.py 已入镜像 =="
VER=$(curl -s --max-time 10 "${BASE}/openapi.json" | jget info.version)
INIMG=$(docker exec zops-backend sh -c 'test -f /app/app/services/ai_dispatcher.py && echo yes || echo no' 2>/dev/null || echo no)
if [ "${VER}" = "0.12.0" ] && [ "${INIMG}" = "yes" ]; then
  ok "版本 ${VER}, ai_dispatcher.py 在容器内"
else
  ng "version=${VER:-不可达} ai_dispatcher=${INIMG} —— scp 后必须 --build backend 且等就绪"
fi

echo "== [2/6] GET /ai/auto: 调度状态真实回显 =="
AUTO=$(curl -s --max-time 10 "${BASE}/api/v1/ai/auto")
AUTOK=$(echo "${AUTO}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]
except Exception:
    print("bad"); raise SystemExit
need=["enabled","running_now","interval_seconds","max_per_tick","max_rounds",
      "llm_configured","model","pending","ticks","last_tick_at",
      "last_scanned","last_analyzed","analyzed_total","errors_total"]
miss=[k for k in need if k not in d]
print("ok" if (not miss and d["enabled"] and d["llm_configured"]
      and d["interval_seconds"]>=30 and 1<=d["max_per_tick"]<=10)
      else "miss=%s enabled=%s cfg=%s" % (miss,d.get("enabled"),d.get("llm_configured")))' 2>&1)
if [ "${AUTOK}" = "ok" ]; then
  ok "开关开、LLM 已配置、interval/max_per_tick/max_rounds 合规; 待办=$(echo "${AUTO}" | jget data.pending)"
else
  ng "${AUTOK} —— llm_configured=False 先查 /ai/config 与 compose 注入"
fi

echo "== [3/6] 补列无损: 老分析记录 trigger_source 回填 MANUAL, 值域干净 =="
TSCHECK=$(curl -s --max-time 15 "${BASE}/api/v1/ai/analyses?limit=200" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
except Exception:
    print("bad"); raise SystemExit
if not items:
    print("empty"); raise SystemExit
vals={it.get("trigger_source","<缺列>") for it in items}
bad=vals-{"AUTO","MANUAL"}
# P9-P11 时代的所有记录都必须是 MANUAL (ALTER 默认值回填)
print("ok total=%d 值域=%s" % (len(items), sorted(vals)) if not bad else "bad=%s"%sorted(bad))')
if [ "${TSCHECK%% *}" = "ok" ]; then
  ok "${TSCHECK#ok } (首轮重建后应全为 MANUAL; 跑过 [4/6] 后出现 AUTO 也合规)"
else
  ng "${TSCHECK} —— 缺列=_ensure_columns 没跑到, 看启动日志"
fi

echo "== [4/6] 真实闭环: stop zops-drill → 归档 → 自动分析 (全程无人工触发) =="
$DOCKER stop zops-drill >/dev/null
echo "    zops-drill 已停止; 等归档循环 (≤60s) 把它收进 problem_records..."
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
if [ -z "${PID}" ]; then
  ng "320s 内没归档到 ACTIVE 的 zops-drill 问题 —— 检查 Zabbix 触发器 25333 与归档循环日志"
else
  echo "    新问题 id=${PID}; 等自动分析出 SUCCESS 记录 (≤480s)..."
  REC=""
  for i in $(seq 1 32); do
    REC=$(curl -s --max-time 15 "${BASE}/api/v1/ai/analyses?problem_record_id=${PID}&status=SUCCESS&limit=5")
    N=$(echo "${REC}" | jget data.total)
    [ "${N:-0}" -ge 1 ] 2>/dev/null && break
    sleep 15
  done
  AUTOJ=$(echo "${REC}" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    print("none"); raise SystemExit
if not its: print("none"); raise SystemExit
it=its[0]
tr=[]
try: tr=json.loads(it.get("tool_trace") or "[]")
except Exception: pass
hostn=len([s for s in tr if str(s.get("tool","")).startswith(("docker_","host_"))])
print("ok" if (it["trigger_source"]=="AUTO" and len(it["analysis"])>=80
      and it["elapsed_ms"]>0 and len(tr)>=1 and hostn>=1)
      else "src=%s len=%d trace=%d host=%d" % (it["trigger_source"],len(it["analysis"]),len(tr),hostn))')
  if [ "${AUTOJ}" = "ok" ]; then
    ok "问题 ${PID}: 无人调用 API, 后台自动完成取证分析 (trigger_source=AUTO, 轨迹含 docker_*/host_)"
    OKPID=${PID}
  else
    ng "${AUTOJ} —— src=MANUAL/None 说明不是后台干的; trace=0/host=0 说明取证没带上 P11 工具"
    OKPID=""
  fi
fi

echo "== [5/6] 不重复排队: 同一问题再等一轮, AUTO 记录仍只有 1 条 =="
if [ -n "${OKPID:-}" ]; then
  echo "    等 75s (跨越一个调度周期)..."
  sleep 75
  DUP=$(curl -s --max-time 15 "${BASE}/api/v1/ai/analyses?problem_record_id=${OKPID}" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["data"]["total"])
except Exception:
    print(-1)')
  TICKS=$(curl -s --max-time 10 "${BASE}/api/v1/ai/auto" | jget data.ticks)
  if [ "${DUP}" = "1" ] && [ "${TICKS:-0}" -ge 2 ] 2>/dev/null; then
    ok "问题 ${OKPID} 分析记录=1 条不重试; 调度已跑 ${TICKS} 轮 (FAILED 不重排同理生效)"
  else
    ng "记录数=${DUP} ticks=${TICKS:-?} —— >1 说明待办口径把已分析问题又排了队"
  fi
else
  ng "[4/6] 没拿到 AUTO 记录, 本项无从验证"
fi

echo "== [6/6] 闸门不放松: 白名单/404/unsupported 老口径全保持 =="
# 重要: [4/6] 把 zops-drill stop 了, 容器停止期间它名下 ~27 个依赖项必然
# unsupported=1 —— 那是数据面真相不是代码回归。所以本项先拉起 drill,
# 等 agent2 再采集一轮 (最多 180s), 让 unsupported 回到"容器全在跑"口径再判。
$DOCKER start zops-drill >/dev/null 2>&1 || true
echo "    已拉起 zops-drill, 等 unsupported 归零 (≤180s)..."
SUP="x"
for i in $(seq 1 12); do
  S=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary" | jget data.item_unsupported)
  if [ "${S:-x}" = "0" ]; then SUP=0; break; fi
  SUP="${S:-x}"
  sleep 15
done
Z1=$(curl -s --max-time 10 "${BASE}/api/v1/ai/tools" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]
    print("clean" if len(d["tools"])==8 and all(t["readonly"] for t in d["tools"]) else "bad")
except Exception:
    print("bad")')
B404=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "${BASE}/api/v1/ai/agent/99999999")
if [ "${Z1}" = "clean" ] && [ "${B404}" = "404" ] && [ "${SUP}" = "0" ]; then
  ok "8 工具只读不变; 不存在归档 404; docker unsupported=0 (drill 拉起后回归; 禁掉的 3 项没复活)"
else
  ng "tools=${Z1} agent404=${B404} unsupported=${SUP} —— 若 unsupported 卡在 27 且 drill 已在跑, 看 docker logs zops-zabbix-agent"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 12 验证通过, 可以进入 Phase 13 (人工确认 + 安全执行)"
  echo "复盘: 告警发生 → 归档 → 自动取证分析 → /ai/analyses 看 AUTO 记录, 全程零人工"
else
  echo "Phase 12 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
