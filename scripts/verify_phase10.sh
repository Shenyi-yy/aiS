#!/usr/bin/env bash
# =============================================================
# Phase 10 验证脚本: Zabbix Tool Calling (Agent 自主取证, 只读白名单)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase10.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: 已 scp 本阶段代码 + Phase 9 已通关(LLM 三要素在 .env);
#           docker compose -p zops up -d --build backend 已执行
# 说明: [4/6] 是多轮大模型往返, 可能要 1-5 分钟, 属正常, 别掐断。
# 诚实原则: 工具轨迹必须真实存在且全部只读, 模型没取证就 FAIL, 不做假 PASS。
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

echo "== [1/6] backend 0.10.0 且 /ai/tools + /ai/agent 路由已注册 =="
OA=$(curl -sf --max-time 10 "${BASE}/openapi.json" || echo "")
VER=$(echo "${OA}" | jget info.version)
HAS=$(echo "${OA}" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
    print("yes" if "/api/v1/ai/tools" in p and "/api/v1/ai/agent/{problem_id}" in p else "no")
except Exception:
    print("no")')
if [ "${VER}" = "0.10.0" ] && [ "${HAS}" = "yes" ]; then
  ok "版本 ${VER}, Agent 路由已挂载"
else
  ng "version=${VER:-不可达} agent路由=${HAS} —— 代码未 scp 或未 --build backend 重建"
fi

echo "== [2/6] 工具白名单: 4 个 zbx_* 只读工具, 无任何写操作 =="
TL=$(curl -s --max-time 10 "${BASE}/api/v1/ai/tools" || echo "")
TLSUM=$(echo "${TL}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]
except Exception:
    print("bad"); raise SystemExit
names=[t["name"] for t in d["tools"]]
ro=all(t["readonly"] for t in d["tools"])
danger=any(k in n.lower() for n in names for k in ("create","delete","update","stop","run"))
print("ok" if len(names)==4 and all(n.startswith("zbx_") for n in names) and ro and not danger and d["max_rounds"]>=1 else "names=%s ro=%s"%(names,ro))')
if [ "${TLSUM}" = "ok" ]; then
  MR=$(echo "${TL}" | jget data.max_rounds)
  ok "4 只读工具 (zbx_list_problem_events/zbx_get_event_detail/zbx_get_host_items/zbx_get_active_triggers), max_rounds=${MR}"
else
  ng "工具白名单校验: ${TLSUM}"
fi

echo "== [3/6] 老库无损迁移: tool_trace 列可用 + Phase 9 归档仍可查 =="
P9=$(curl -s --max-time 10 "${BASE}/api/v1/ai/analyses?limit=50" || echo "")
P9CHK=$(echo "${P9}" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
except Exception:
    print("bad"); raise SystemExit
succ=[it for it in items if it["status"]=="SUCCESS"]
# P9 老记录必须还能序列化出来(说明补列成功), 且 tool_trace 为空串
if succ and all(isinstance(it.get("tool_trace"), str) for it in items):
    print("ok %d" % len(succ))
else:
    print("bad items=%d" % len(items))')
if [ "${P9CHK%% *}" = "ok" ]; then
  ok "tool_trace 列已就绪, Phase 9 的 ${P9CHK##* } 条成功分析仍可查"
else
  ng "迁移校验: ${P9CHK} —— 后端日志应有 '已补列 tool_trace'; 若 500 说明代码没更新到位"
fi

echo "== [4/6] 真实 Agent 分析: 模型自主调工具取证 (多轮往返, 耐心等) =="
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
  AG=""
else
  echo "    选用问题 id=${PID}; 开始 Agent 分析 (curl 超时 600s)..."
  AG=$(curl -s --max-time 600 -X POST "${BASE}/api/v1/ai/agent/${PID}" || echo "")
fi
AST=$(echo "${AG}" | jget data.status)
ALEN=$(echo "${AG}" | python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin)["data"]["analysis"]))
except Exception:
    print(0)')
if [ "${AST}" = "SUCCESS" ] && [ "${ALEN}" -ge 80 ]; then
  ok "Agent 分析完成: 正文 ${ALEN} 字符, 耗时 $(echo "${AG}" | jget data.elapsed_ms)ms"
else
  ng "status=${AST:-?} 正文=${ALEN} detail=$(echo "${AG}" | jget detail)"
fi

echo "== [5/6] 工具轨迹真实且全只读 (核心门禁: 没取证=不合格) =="
TCHK=$(echo "${AG}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]
    tr=json.loads(d["tool_trace"])
except Exception:
    print("bad"); raise SystemExit
if not isinstance(tr, list) or len(tr) < 1:
    print("no_tool_calls=%s" % len(tr) if isinstance(tr,list) else "bad"); raise SystemExit
names={s["tool"] for s in tr}
if not all(n.startswith("zbx_") for n in names):
    print("nonreadonly=%s" % names); raise SystemExit
if not any(s.get("args") for s in tr):
    print("empty_args"); raise SystemExit
ok_n=len([s for s in tr if s.get("ok")])
print("ok %d %s ok%d" % (len(tr), ",".join(sorted(names)), ok_n))')
if [ "${TCHK%% *}" = "ok" ]; then
  ok "轨迹 ${TCHK#ok } (调用次数/工具名/成功数) —— 结论出自真实取证"
else
  ng "轨迹校验: ${TCHK} —— 若 no_tool_calls, 说明模型没调工具(换支持 function-calling 的模型或加大轮数); 若 502 见上一项 detail"
fi

echo "== [6/6] 诚实边界 + 老链路无损 =="
B404=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "${BASE}/api/v1/ai/agent/99999999")
SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary" || echo "")
UP=$(echo "${SUM}" | jget data.item_unsupported)
ER=$(echo "${SUM}" | jget data.item_with_error)
if [ "${B404}" = "404" ] && [ "${UP:-x}" = "0" ] && [ "${ER:-x}" = "0" ]; then
  ok "不存在的归档问题返 404 (不编造分析); docker 收数 unsupported=0 error=0"
else
  ng "agent(不存在)=${B404} (期望404) unsupported=${UP:-?} error=${ER:-?}"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 10 验证通过, 可以进入 Phase 11 (Docker/Linux Tool Calling)"
  echo "复盘: tool_trace 里每一步都是模型自己决定调的只读工具, 全过程可审计"
else
  echo "Phase 10 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
