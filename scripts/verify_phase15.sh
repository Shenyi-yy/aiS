#!/usr/bin/env bash
# =============================================================
# Phase 15 验证脚本: RAG 知识库 (BM25 检索 + 闭环复盘 + Agent 注入)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase15.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: Phase 14 已通关; 已 scp 本阶段代码 + up -d --build backend;
#           zops-drill 容器与触发器 25333 存在; AI_AUTO_ENABLED 未关;
#           库中已有至少一条"分析成功且被批准执行过"的真实闭环
#           (P13/P14 的 verify 跑过就有; [3/6] 的复盘只从这种真数据生成)。
# 关键门禁: [3/6] 复盘诚实闸门 —— 不存在的问题 404, 缺环拒绝, 绝不生成
#           想象中的复盘; [5/6] 新一轮真实告警的 AUTO 分析必须带
#           round=0 的 rag_retrieve 且 hits>=1 —— 知识库真的喂进了模型。
# 防陈旧: [5/6] 先取归档 id 基线, 只认 id>基线 的本轮新归档 (P14 教训)。
# 破坏性: 会 stop/start zops-drill (不碰核心 5 容器); trap 保证拉回。
#         [3/6] 生成一条真实复盘 (幂等: 重跑不产生重复语料)。
# 说明: [0] 自带 /health 就绪等待; [6/6] drill 拉起后轮询容器系 unsupported
#       归零, 镜像系模板噪音单列不卡闸。全程约 5-12 分钟 (含一次 LLM 往返)。
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

echo "== [1/6] backend 0.15.0 + 知识库四件套已入镜像 + 路由注册 =="
VER=$(curl -s --max-time 10 "${BASE}/openapi.json" | jget info.version)
INIMG=$(docker exec zops-backend sh -c 'for f in app/services/knowledge.py app/services/knowledge_tools.py app/schemas/knowledge.py app/api/routes/knowledge.py; do test -f /app/$f || exit 1; done; echo yes' 2>/dev/null || echo no)
HASROUTE=$(curl -s --max-time 10 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
except Exception:
    print("no"); raise SystemExit
need={"/api/v1/knowledge","/api/v1/knowledge/search","/api/v1/knowledge/case/{problem_id}","/api/v1/knowledge/{entry_id}"}
print("yes" if need <= set(p) else "no")')
if [ "${VER}" = "0.15.0" ] && [ "${INIMG}" = "yes" ] && [ "${HASROUTE}" = "yes" ]; then
  ok "版本 ${VER}, 检索服务/工具/出入参/路由四件套在容器内, /knowledge 路由已注册"
else
  ng "version=${VER:-不可达} files=${INIMG} routes=${HASROUTE} —— scp 后必须 up -d --build backend"
fi

echo "== [2/6] 红线复核: 工具白名单 9 个全只读 (含 kb_search), /audit 面仍零写方法 =="
TOOLCHK=$(curl -s --max-time 10 "${BASE}/api/v1/ai/tools" | python3 -c 'import json,sys
try:
    ts=json.load(sys.stdin)["data"]["tools"]
except Exception:
    print("bad"); raise SystemExit
names=[t["name"] for t in ts]
pre=all(n.startswith(("zbx_","docker_","host_","kb_")) for n in names)
ro=all(t["readonly"] is True for t in ts)
print("ok" if len(names)==9 and pre and ro and "kb_search" in names else "bad:%s" % names)')
AUDGET=$(curl -s --max-time 10 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    p=json.load(sys.stdin)["paths"]
except Exception:
    print("bad"); raise SystemExit
w=0
for path,methods in p.items():
    if path.startswith("/api/v1/audit"):
        w+=len(set(methods)-{"get"})
print("clean" if w==0 else "dirty:%d" % w)')
if [ "${TOOLCHK}" = "ok" ] && [ "${AUDGET}" = "clean" ]; then
  ok "9 个工具全只读且 kb_search 在闸内; 审计面依旧只有 GET —— 知识库没给 AI 开执行旁路"
else
  ng "tools=${TOOLCHK} audit=${AUDGET} —— 期望 ok/clean"
fi

echo "== [3/6] 复盘诚实闸门: 不存在 404 + 真实闭环出复盘 + 幂等 =="
GONE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "${BASE}/api/v1/knowledge/case/999999")
PID3=$(curl -s --max-time 10 "${BASE}/api/v1/exec/requests?status=EXECUTED&limit=200" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
linked=[x for x in its if x.get("problem_record_id")]
print(linked[0]["problem_record_id"] if linked else "")')
CASE3=""
if [ "${GONE}" != "404" ] || [ -z "${PID3}" ]; then
  ng "case/999999=${GONE} (期望 404); EXECUTED 关联问题=${PID3:-无} —— 先跑过 P13/P14 演练再验本节"
else
  R1=$(curl -s --max-time 15 -X POST "${BASE}/api/v1/knowledge/case/${PID3}")
  R2=$(curl -s --max-time 15 -X POST "${BASE}/api/v1/knowledge/case/${PID3}")
  CASE3=$(python3 -c 'import json,sys
r1=json.loads(sys.argv[1]); r2=json.loads(sys.argv[2])
d1=r1.get("data") or {}; d2=r2.get("data") or {}
e1=d1.get("entry") or {}; e2=d2.get("entry") or {}
good=(r1.get("code")==0 and e1.get("id")
      and e1.get("category")=="CASE" and e1.get("source_type")=="CLOSED_LOOP"
      and e1.get("problem_record_id")==int(sys.argv[3])
      and "AI 分析" in e1.get("content",""))
idem=(r2.get("code")==0 and d2.get("created") is False
      and e2.get("id")==e1.get("id"))
print("ok" if good and idem else "bad:code=%s created=%s cat=%s idem=%s" % (r1.get("code"), d1.get("created"), e1.get("category"), idem))' "${R1}" "${R2}" "${PID3}")
fi
if [ "${CASE3:-}" = "ok" ]; then
  ok "问题 ${PID3}: 不存在→404 拦下; 真实闭环→复盘生成 (content 内嵌真实分析与处置); 重跑幂等不重复入语料"
else
  ng "missing404=${GONE} case=${CASE3:-未跑} —— 期望 404/ok"
fi

echo "== [4/6] BM25 检索行为: 真实查询有命中且给分, 乱码查询诚实零命中 =="
S1=$(curl -s --max-time 15 "${BASE}/api/v1/knowledge/search" --get --data-urlencode "q=zops-drill 容器 停止" --data-urlencode "top_k=3")
S2=$(curl -s --max-time 15 "${BASE}/api/v1/knowledge/search" --get --data-urlencode "q=量子隧穿磁单极" --data-urlencode "top_k=3")
SEARCH=$(python3 -c 'import json,sys
a=json.loads(sys.argv[1]).get("data") or {}
b=json.loads(sys.argv[2]).get("data") or {}
hits=a.get("hits") or []
good=(a.get("corpus_total",0)>=1 and a.get("count",0)>=1 and hits
      and hits[0]["score"]>0 and "zops-drill" in (hits[0]["entry"]["title"]+hits[0]["entry"]["content"]+hits[0]["entry"]["tags"]))
print("ok" if good and b.get("count")==0 else "bad:hits=%s junk=%s" % (a.get("count"), b.get("count")))
print("top1=%s score=%s" % ((hits[0]["entry"]["title"] if hits else "-"), (hits[0]["score"] if hits else "-")))
print("corpus=%s" % a.get("corpus_total"))' "${S1}" "${S2}")
SFLAG=$(echo "${SEARCH}" | head -1)
if [ "${SFLAG}" = "ok" ]; then
  ok "$(echo "${SEARCH}" | tr '\n' ' ' | sed 's/ok //') —— 命中给分可解释, 无关查询不硬凑"
else
  ng "${SEARCH}"
fi

echo "== [5/6] 真实闭环注入: stop drill → 新告警 AUTO 分析必须带 rag_retrieve(hits>=1) → 否决路径 =="
BASE_MAX=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items=json.load(sys.stdin)["data"]["items"]
except Exception:
    items=[]
print(max([it["id"] for it in items], default=0))')
$DOCKER stop zops-drill >/dev/null
echo "    zops-drill 已停止 (归档基线 id=${BASE_MAX}); 等新 ACTIVE 归档 (<=320s) → AUTO 分析 (<=600s)..."
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
RAGOK=""
if [ -z "${PID}" ]; then
  ng "320s 内没归档到 id>${BASE_MAX} 的新 ACTIVE zops-drill 问题 —— 查触发器 25333 与归档循环日志"
else
  for i in $(seq 1 40); do
    RAGOK=$(curl -s --max-time 15 "${BASE}/api/v1/ai/analyses?problem_record_id=${PID}&status=SUCCESS&limit=10" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
auto=[x for x in its if x["trigger_source"]=="AUTO"]
if not auto:
    print(""); raise SystemExit
tr=json.loads(auto[0].get("tool_trace") or "[]")
r0=[t for t in tr if t.get("round")==0 and t.get("tool")=="rag_retrieve"]
print("ok" if r0 and r0[0].get("hits",0)>=1 and r0[0].get("ok") is True else "")')
    [ -n "${RAGOK}" ] && break
    sleep 15
  done
fi
if [ "${RAGOK}" != "ok" ]; then
  ng "问题 ${PID:-?}: 600s 内没等到 AUTO 分析, 或 tool_trace 缺 round=0 rag_retrieve/hits<1 —— 知识库没喂进模型 (查 [3/6] 复盘是否生成、backend 日志)"
else
  REJ=""
  REQ=$(curl -s --max-time 10 "${BASE}/api/v1/exec/requests?problem_record_id=${PID}&status=PENDING_APPROVAL" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
auto=[x for x in its if x["source"]=="AUTO"]
print(auto[0]["id"] if auto else "")')
  if [ -z "${REQ}" ]; then
    for i in $(seq 1 12); do
      sleep 15
      REQ=$(curl -s --max-time 10 "${BASE}/api/v1/exec/requests?problem_record_id=${PID}&status=PENDING_APPROVAL" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
except Exception:
    its=[]
auto=[x for x in its if x["source"]=="AUTO"]
print(auto[0]["id"] if auto else "")')
      [ -n "${REQ}" ] && break
    done
  fi
  if [ -n "${REQ}" ]; then
    RC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST "${BASE}/api/v1/exec/requests/${REQ}/reject" \
      -H 'Content-Type: application/json' \
      -d '{"approver":"verify15","reason":"演练: 本轮只验知识库注入, 处置留给人工决定"}')
    [ "${RC}" = "200" ] && REJ=ok
    echo "    问题 ${PID}: rag_retrieve 已进 tool_trace; 否决建议单 ${REQ} → HTTP ${RC}"
  fi
  $DOCKER start zops-drill >/dev/null 2>&1
  if [ "${REJ}" = "ok" ]; then
    ok "新告警 ${PID} 的 AUTO 分析真实带出 round=0 rag_retrieve(hits>=1), 建议单 ${REQ} 已人工否决 —— 检索在用, 执行仍只走人"
  else
    ng "否决单失败 (REQ=${REQ:-无} RC=${RC:-?}) —— 期望分析后按目录生成 AUTO 建议单 (P13 链路?)"
  fi
fi

echo "== [6/6] 审计链收口: 重放 ok + knowledge.created 入链 + execution.rejected 入链 + 数据面回归 =="
VF=$(curl -s --max-time 30 "${BASE}/api/v1/audit/verify")
VOK=$(echo "${VF}" | jget data.ok)
KCT=$(curl -s --max-time 10 "${BASE}/api/v1/audit?event_type=knowledge.created&limit=5" | jget data.total)
RJT=$(curl -s --max-time 10 "${BASE}/api/v1/audit?event_type=execution.rejected&limit=5" | jget data.total)
SUP="x"; IMG="?"
for i in $(seq 1 8); do
  SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary")
  S=$(echo "${SUM}" | jget data.item_unsupported)
  IMG=$(echo "${SUM}" | jget data.item_unsupported_image)
  if [ "${S:-x}" = "0" ]; then SUP=0; break; fi
  SUP="${S:-x}"
  sleep 15
done
VREASON=$(echo "${VF}" | jget data.reason)
if [ "${VOK}" = "True" ] && [ "${KCT:-0}" -ge 1 ] 2>/dev/null && [ "${RJT:-0}" -ge 1 ] 2>/dev/null && [ "${SUP}" = "0" ]; then
  ok "哈希链重放 ok (checked=$(echo "${VF}" | jget data.checked), 知识库写入没弄断链); knowledge.created=${KCT} execution.rejected=${RJT} 已入链; 容器系 unsupported=0 (镜像系噪音=${IMG} 单列不卡闸)"
else
  ng "verify=${VOK:-?}(${VREASON:-}) knowledge.created=${KCT:-0} execution.rejected=${RJT:-0} unsupported(容器系)=${SUP} image=${IMG:-?} —— 期望 True/>=1/>=1/0"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 15 验证通过, 可以进入 Phase 16 (平台完善)"
  echo "复盘: BM25 检索双通道 (round=0 注入 + kb_search 工具), 复盘只从真实闭环生成, 知识写操作入审计链"
else
  echo "Phase 15 未通过: 按上面 [FAIL] 行排查后重跑本脚本"
fi
