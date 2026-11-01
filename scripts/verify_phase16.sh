#!/usr/bin/env bash
# =============================================================
# Phase 16 验证脚本: 平台完善 —— 端到端非破坏巡检 (全平台总闸)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase16.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: Phase 15 已通关; 已 scp 本阶段代码 + up -d --build backend。
# 定位: 不再演练新能力, 而是对 P1-P16 全链路做一次"体检":
#       大盘全闭环可视化 / 五域 API 齐 / 审计链完好 / 各域读通 /
#       安全红线不回归 / 调度与数据面健康。
# 非破坏: 全程零 stop/start, 零写接口调用 —— 随时可跑, 跑多少遍都不改状态。
# 说明: [0] 自带 /health 就绪等待; [6/6] 容器系 unsupported 轮询归零
#       (上一轮演练遗留也能等到), 镜像系模板噪音单列不卡闸。全程 1-3 分钟。
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

echo "== [0] 等待后端就绪 (防抢跑) =="
READY=no
for i in $(seq 1 30); do
  if curl -sf --max-time 3 "${BASE}/health" >/dev/null 2>&1; then READY=yes; break; fi
  sleep 3
done
if [ "${READY}" != "yes" ]; then
  echo "  后端 90s 未就绪, 后续检查必然全挂 —— 看 docker logs zops-backend"
fi

echo "== [1/6] backend 0.16.0 + 大盘闭环面板齐 (四面板+时间线, 全真实数据源) =="
VER=$(curl -s --max-time 10 "${BASE}/openapi.json" | jget info.version)
DASH=$(curl -s --max-time 10 "${BASE}/")
DMARK=$(python3 -c 'import sys
h=sys.stdin.read()
need=["panels-closed-loop","ai-rows","exec-rows","audit-rows","kb-hits","panel-tl","btn-tl","loadTimeline","kbSearch"]
miss=[m for m in need if m not in h]
print("ok" if not miss else "missing:%s" % ",".join(miss))' <<< "${DASH}")
if [ "${VER}" = "0.16.0" ] && [ "${DMARK}" = "ok" ]; then
  ok "版本 0.16.0; 大盘含 AI 分析/审批单/审计链/知识库检索/问题时间线五块闭环视图"
else
  ng "version=${VER:-不可达} dash=${DMARK} —— scp 后必须 up -d --build backend"
fi

echo "== [2/6] 五域 API 面完整: 监控归档 / AI / 执行 / 审计 / 知识库 =="
API5=$(curl -s --max-time 10 "${BASE}/openapi.json" | python3 -c 'import json,sys
try:
    p=set(json.load(sys.stdin)["paths"])
except Exception:
    print("bad"); raise SystemExit
need={"/api/v1/problems","/api/v1/ai/tools","/api/v1/ai/agent/{problem_id}",
      "/api/v1/exec/requests","/api/v1/audit","/api/v1/audit/verify",
      "/api/v1/knowledge","/api/v1/knowledge/search"}
miss=need-p
print("ok" if not miss else "missing:%s" % ",".join(sorted(miss)))')
if [ "${API5}" = "ok" ]; then
  ok "五域代表端点全部在册 (problems / ai / exec / audit / knowledge)"
else
  ng "${API5}"
fi

echo "== [3/6] 审计链完好: 重放 ok + 本次 0.16.0 启动已入链 =="
VF=$(curl -s --max-time 30 "${BASE}/api/v1/audit/verify")
VOK=$(echo "${VF}" | jget data.ok)
SU=$(curl -s --max-time 10 "${BASE}/api/v1/audit?event_type=system.startup&limit=3")
SUVER=$(echo "${SU}" | python3 -c 'import json,sys
try:
    its=json.load(sys.stdin)["data"]["items"]
    print(its[0]["detail"].get("version","") if its else "")
except Exception:
    print("")')
if [ "${VOK}" = "True" ] && [ "${SUVER}" = "0.16.0" ]; then
  ok "哈希链重放 ok (checked=$(echo "${VF}" | jget data.checked)); 0.16.0 启动事件在链上 (开关状态可回溯)"
else
  ng "verify=${VOK:-?} reason=$(echo "${VF}" | jget data.reason) startup=${SUVER:-无} —— 期望 True/0.16.0"
fi

echo "== [4/6] 各域 GET 一跳全通 (只读巡检, 零副作用) =="
ALL200=$(python3 -c 'import json,subprocess,sys
urls=["/api/v1/problems?limit=1","/api/v1/ai/analyses?limit=1","/api/v1/ai/auto",
      "/api/v1/exec/requests?limit=1","/api/v1/exec/operations",
      "/api/v1/audit?limit=1","/api/v1/knowledge?limit=1","/api/v1/docker/summary",
      "/api/v1/system/info","/api/v1/hosts"]
bad=[]
for u in urls:
    r=subprocess.run(["curl","-s","--max-time","20",sys.argv[1]+u],capture_output=True,text=True)
    try:
        d=json.loads(r.stdout)
        if d.get("code")!=0: bad.append(u+" code="+str(d.get("code")))
    except Exception:
        bad.append(u+" 非JSON/HTTP错")
print("ok" if not bad else "bad:%s" % ";".join(bad))' "${BASE}")
if [ "${ALL200}" = "ok" ]; then
  ok "10 个只读端点全部 code=0 (大盘每张面板背后的数据源都活着)"
else
  ng "${ALL200}"
fi

echo "== [5/6] 安全红线不回归: 9 工具全只读 / 审计面零写 / 复盘无手工口 / 审批恒 required =="
REDLINE=$(python3 -c 'import json,subprocess,sys
base=sys.argv[1]
def g(u):
    r=subprocess.run(["curl","-s","--max-time","20",base+u],capture_output=True,text=True)
    return json.loads(r.stdout)
fails=[]
ts=g("/api/v1/ai/tools")["data"]["tools"]
names=[t["name"] for t in ts]
if len(names)!=9 or not all(t["readonly"] for t in ts) or "kb_search" not in names:
    fails.append("tools=%s" % names)
oa=json.loads(subprocess.run(["curl","-s","--max-time","20",base+"/openapi.json"],capture_output=True,text=True).stdout)["paths"]
w=sum(len(set(m)-{"get"}) for pth,m in oa.items() if pth.startswith("/api/v1/audit"))
if w: fails.append("audit 面出现 %d 个写方法" % w)
# 知识条目 category 正则不收 CLOSED_LOOP: 手工伪造复盘的路径必须 422
r=subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","--max-time","20",
  "-X","POST",base+"/api/v1/knowledge","-H","Content-Type: application/json",
  "-d",json.dumps({"title":"红线探针","content":"x","category":"CLOSED_LOOP"})],
  capture_output=True,text=True)
if r.stdout!="422": fails.append("伪造复盘探针=%s (期望 422)" % r.stdout)
cfg=g("/api/v1/exec/config")["data"]
if not cfg["approval_required"]: fails.append("approval_required=false 红线!")
print("ok" if not fails else ";".join(fails))' "${BASE}")
if [ "${REDLINE}" = "ok" ]; then
  ok "AI 无执行路径 / 审计只追加 / 复盘只从闭环生成 / 执行必人工批准 —— 四条红线全在"
else
  ng "${REDLINE}"
fi

echo "== [6/6] 运行时健康: LLM 与调度在线 / 演练容器恢复 / 容器系 unsupported 归零 =="
AUTO=$(curl -s --max-time 10 "${BASE}/api/v1/ai/auto")
AEN=$(echo "${AUTO}" | jget data.enabled); ALLC=$(echo "${AUTO}" | jget data.llm_configured)
TICK=$(echo "${AUTO}" | jget data.ticks)
SUP="x"; IMG="?"
for i in $(seq 1 8); do
  SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary")
  S=$(echo "${SUM}" | jget data.item_unsupported)
  IMG=$(echo "${SUM}" | jget data.item_unsupported_image)
  if [ "${S:-x}" = "0" ]; then SUP=0; break; fi
  SUP="${S:-x}"
  sleep 15
done
if [ "${ALLC}" = "True" ] && [ "${AEN}" = "True" ] && [ "${TICK:-0}" -ge 1 ] 2>/dev/null && [ "${SUP}" = "0" ]; then
  ok "LLM configured + 自动调度已跑 ${TICK} 轮; 容器系 unsupported=0 (镜像系噪音=${IMG} 单列不卡闸) —— 平台整体在真实运转"
else
  ng "llm=${ALLC:-?} auto=${AEN:-?} tick=${TICK:-0} unsupported(容器系)=${SUP} image=${IMG:-?} —— 期望 True/True/>=1/0"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 16 验证通过 —— 全平台 16 个 Phase 通关: 监控→告警→取数→AI分析→定位→建议→人工确认→安全执行→审计→知识沉淀→大盘闭环可视化"
else
  echo "Phase 16 未通过: 按上面 [FAIL] 行排查后重跑本脚本 (非破坏, 可反复跑)"
fi
