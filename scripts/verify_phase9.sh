#!/usr/bin/env bash
# =============================================================
# Phase 9 验证脚本: LLM Agent 接入 (真实大模型往返 + 分析归档)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase9.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: 已 scp 本阶段全部代码; /opt/zops/.env 已填 LLM_BASE_URL/
#           LLM_API_KEY/LLM_MODEL; 已 docker compose -p zops up -d --build backend
#           (问题归档里至少有一条真实告警 —— Phase 8 演练已保证)
# 诚实原则: LLM 未配置/调不通一律 FAIL, 脚本不接受任何假成功。
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

echo "== [1/6] backend 0.9.0 且 /api/v1/ai/* 路由已注册 =="
OA=$(curl -sf --max-time 10 "${BASE}/openapi.json" || echo "")
VER=$(echo "${OA}" | jget info.version)
HAS_AI=$(echo "${OA}" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
    print("yes" if "/api/v1/ai/analyses" in p and "/api/v1/ai/analyze/{problem_id}" in p else "no")
except Exception:
    print("no")')
if [ "${VER}" = "0.9.0" ] && [ "${HAS_AI}" = "yes" ]; then
  ok "版本 ${VER}, AI 路由已挂载 (/config /check /analyze /analyses)"
else
  ng "version=${VER:-不可达} ai路由=${HAS_AI} —— 代码未 scp 或未 --build backend 重建"
fi

echo "== [2/6] LLM 三要素已注入容器 (只查长度与形状, 不打印 Key) =="
CFG=$(curl -sf --max-time 10 "${BASE}/api/v1/ai/config" || echo "")
CONF=$(echo "${CFG}" | jget data.configured)
KLEN=$(echo "${CFG}" | jget data.api_key_len)
MODEL=$(echo "${CFG}" | jget data.model)
BURL=$(echo "${CFG}" | jget data.base_url)
if [ "${CONF}" = "True" ] && [ "${KLEN:-0}" -gt 0 ] && [ -n "${MODEL}" ] && \
   echo "${BURL}" | grep -q '^http'; then
  ok "base_url=${BURL} model=${MODEL} key_len=${KLEN} (Key 本体不进响应)"
else
  ng "configured=${CONF:-?} key_len=${KLEN:-?} model='${MODEL:-?}' base='${BURL:-?}'"
  echo "        → 编辑 /opt/zops/.env 填 LLM_BASE_URL(到/v1为止)/LLM_API_KEY/LLM_MODEL,"
  echo "          然后 docker compose -p zops up -d backend (重建容器才注入新env)"
fi

echo "== [3/6] 真实连通性: POST /api/v1/ai/check (最小往返, 不接受假 OK) =="
CHK=$(curl -s --max-time 90 -X POST "${BASE}/api/v1/ai/check" || echo "")
COK=$(echo "${CHK}" | jget data.ok)
LAT=$(echo "${CHK}" | jget data.latency_ms)
if [ "${COK}" = "True" ]; then
  ok "模型真实应答, 往返 ${LAT}ms"
else
  DET=$(echo "${CHK}" | jget detail)
  ng "check 失败: ${DET:-无响应} —— 核对 Key 有效性/余额/base_url 是否带 /v1"
fi

echo "== [4/6] 挑一条真实归档问题跑故障分析 (大模型要 10-60s, 耐心等) =="
PID=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items = json.load(sys.stdin)["data"]["items"]
except Exception:
    items = []
drill = [it for it in items if "zops-drill" in it["trigger_name"]]
pick = (drill or items)
print(pick[0]["id"] if pick else "")')
if [ -z "${PID}" ]; then
  ng "problem_records 是空表 —— 先 POST /api/v1/problems/sync 或跑一轮 fault_drill.sh"
  ANA=""
else
  echo "    选用问题 id=${PID} (演练归档优先)"
  ANA=$(curl -s --max-time 240 -X POST "${BASE}/api/v1/ai/analyze/${PID}" || echo "")
fi
ASTAT=$(echo "${ANA}" | jget data.status)
ALEN=$(echo "${ANA}" | python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin)["data"]["analysis"]))
except Exception:
    print(0)')
if [ "${ASTAT}" = "SUCCESS" ] && [ "${ALEN}" -ge 80 ]; then
  ok "分析完成: status=${ASTAT} 正文 ${ALEN} 字符 (非模板假文)"
else
  ng "status=${ASTAT:-?} 正文长度=${ALEN} detail=$(echo "${ANA}" | jget detail)"
fi

echo "== [5/6] 分析已落归档表 (可追溯, 模型名与配置一致) =="
LST=$(curl -s --max-time 10 "${BASE}/api/v1/ai/analyses?limit=5" || echo "")
TOTAL=$(echo "${LST}" | jget data.total)
ROW=$(echo "${LST}" | python3 -c 'import json,sys
try:
    items = json.load(sys.stdin)["data"]["items"]
except Exception:
    items = []
for it in items:
    if it["status"] == "SUCCESS" and len(it["analysis"]) >= 80:
        print(it["problem_record_id"], it["model"], it["elapsed_ms"])
        break
else:
    print("")')
R_PID=$(echo "${ROW}" | awk '{print $1}')
R_MODEL=$(echo "${ROW}" | awk '{print $2}')
R_MS=$(echo "${ROW}" | awk '{print $3}')
if [ -n "${R_PID}" ] && [ "${R_PID}" = "${PID:-}" ] && [ "${R_MODEL}" = "${MODEL}" ] && \
   [ "${R_MS:-0}" -gt 0 ]; then
  ok "归档 total=${TOTAL}, 最新成功记录 problem=${R_PID} model=${R_MODEL} ${R_MS}ms"
else
  ng "total=${TOTAL:-?} 匹配行='${ROW:-无}' (期望 problem=${PID:-?} model=${MODEL:-?})"
fi

echo "== [6/6] 诚实边界 + 老链路无损 =="
B404=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST "${BASE}/api/v1/ai/analyze/99999999")
# 泄漏检查: /config 响应里只许出现 api_key_len, 不许出现 "api_key" 字段本体
LEAK=$(echo "${CFG}" | grep -c '"api_key"' || true)
SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary" || echo "")
UP=$(echo "${SUM}" | jget data.item_unsupported)
ER=$(echo "${SUM}" | jget data.item_with_error)
if [ "${B404}" = "404" ] && [ "${LEAK:-1}" = "0" ] && \
   [ "${UP:-x}" = "0" ] && [ "${ER:-x}" = "0" ]; then
  ok "不存在的问题返 404 (不编造分析); 响应体无 Key; docker 收数 unsupported=0 error=0"
else
  ng "analyze(不存在)=${B404} (期望404) key泄漏命中=${LEAK} unsupported=${UP:-?} error=${ER:-?}"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 9 验证通过, 可以进入 Phase 10 (Zabbix Tool Calling)"
  echo "复盘: 大盘/接口里的分析正文由 ${MODEL} 真实生成, 证据快照见 ai_analysis_records.input_snapshot"
else
  echo "Phase 9 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
