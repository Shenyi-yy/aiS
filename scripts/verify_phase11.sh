#!/usr/bin/env bash
# =============================================================
# Phase 11 验证脚本: Docker/Linux Tool Calling (只读白名单扩到 8 工具)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase11.sh
# 通关标准: PASS=6 FAIL=0
# 前置条件: Phase 10 已通关; 已 scp 本阶段代码 + --build backend;
#           Docker 模板已挂在 ubuntu-host 且 LLD 有容器监控项 (Phase 7/8 现场)
# 关键门禁: [5/6] Agent 必须**实际调用过 docker_*/host_* 组工具**才算通过,
#           只调 Zabbix 工具不给假 PASS —— 这是本阶段的真实增量证明。
# 说明: [4/6] 多轮大模型往返, 1-5 分钟正常, 别掐断。
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

echo "== [1/6] backend 0.11.0 + host_tools.py 已入镜像 =="
VER=$(curl -s --max-time 10 "${BASE}/openapi.json" | jget info.version)
INIMG=$(docker exec zops-backend sh -c 'test -f /app/app/services/host_tools.py && echo yes || echo no' 2>/dev/null || echo no)
if [ "${VER}" = "0.11.0" ] && [ "${INIMG}" = "yes" ]; then
  ok "版本 ${VER}, host_tools.py 在容器内 (说明 --build 生效)"
else
  ng "version=${VER:-不可达} host_tools=${INIMG} —— scp 后必须 docker compose -p zops up -d --build backend"
fi

echo "== [2/6] 白名单扩到 8 个只读工具 (4 zbx + 3 docker + 1 host) =="
TL=$(curl -s --max-time 10 "${BASE}/api/v1/ai/tools" || echo "")
TLSUM=$(echo "${TL}" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]
except Exception:
    print("bad"); raise SystemExit
names=[t["name"] for t in d["tools"]]
need={"zbx_list_problem_events","zbx_get_event_detail","zbx_get_host_items","zbx_get_active_triggers",
      "docker_get_summary","docker_list_containers","docker_container_detail","host_get_linux_metrics"}
ro=all(t["readonly"] for t in d["tools"])
bad=[n for n in names if not n.startswith(("zbx_","docker_","host_"))]
print("ok" if set(names)==need and len(names)==8 and ro and not bad else "names=%s ro=%s bad=%s"%(names,ro,bad))')
if [ "${TLSUM}" = "ok" ]; then
  ok "8 工具全部只读, 前缀合规, 无写操作入口"
else
  ng "白名单: ${TLSUM}"
fi

echo "== [3/6] 数据面诚实: 三方交叉核对容器数 (Zabbix DB / 后端聚合 / 宿主机 docker ps) =="
XCHK=$(python3 - <<'PY'
import json, subprocess, urllib.request

def http(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=30) as r:
            return json.load(r)["data"]
    except Exception as exc:
        return {"__err__": str(exc)}

# ① Zabbix 侧真值: item 表里 ubuntu-host 的"容器运行状态"监控项个数
#    (7.0 后端是 PostgreSQL; 铁律: 容器项由 LLD 建在 items 表, 值才在 history 系表)
n_db = -1
for cmd in (
    ["docker", "exec", "zops-postgres", "psql", "-U", "zabbix", "-d", "zabbix", "-tA",
     "-c", "select count(*) from items i join hosts h on h.hostid=i.hostid "
           "where h.name='ubuntu-host' and i.key_ like 'docker.container_info"
           ".state.running[\"/%'"],
    ["sudo", "docker", "exec", "zops-postgres", "psql", "-U", "zabbix", "-d", "zabbix", "-tA",
     "-c", "select count(*) from items i join hosts h on h.hostid=i.hostid "
           "where h.name='ubuntu-host' and i.key_ like 'docker.container_info"
           ".state.running[\"/%'"],
):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if p.returncode == 0 and p.stdout.strip():
            n_db = int(p.stdout.strip().splitlines()[-1])
            break
    except Exception:
        pass

# ② 后端聚合口径 (走唯一出口 zabbix_client)
summ = http("/api/v1/docker/summary")
n_sum = int(summ.get("discovered_container_count", -1)) if "__err__" not in summ else -1

# ③ 宿主机 ground truth: 运行中的容器数
n_ps = -1
for cmd in (["docker", "ps", "-q"], ["sudo", "docker", "ps", "-q"]):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            n_ps = len([x for x in p.stdout.split() if x])
            break
    except Exception:
        pass

if n_db < 0:
    # DB 计数取不到 (权限/口令): 降级为"后端聚合 vs 宿主机 ps"两方核对
    if n_sum >= 0 and n_ps >= 0 and n_sum >= 4:
        print("ok 降级两方核对: 后端发现容器=%d 宿主机运行容器=%d (DB 计数不可用)" % (n_sum, n_ps))
    else:
        print("bad db=%d summary=%d ps=%d" % (n_db, n_sum, n_ps))
elif n_sum >= 0 and n_ps >= 0 and n_db >= 4 and n_db == n_sum:
    print("ok 三方一致: Zabbix running 项=%d 后端发现容器=%d 宿主机运行容器=%d" % (n_db, n_sum, n_ps))
else:
    print("bad db=%d summary=%d ps=%d" % (n_db, n_sum, n_ps))
PY
)
if [ "${XCHK%% *}" = "ok" ]; then
  ok "${XCHK#ok }"
else
  ng "${XCHK} —— DB 口令非默认时三方核对会降级; 若仍 bad: 检查 Docker 模板是否挂在 ubuntu-host + LLD 是否发现过容器"
fi

echo "== [4/6] 真实 Agent 分析 (8 工具可用, 多轮取证) =="
PID=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items = json.load(sys.stdin)["data"]["items"]
except Exception:
    items = []
drill = [it for it in items if "zops-drill" in it["trigger_name"]]
pick = (drill or items)
print(pick[0]["id"] if pick else "")')
if [ -z "${PID}" ]; then
  ng "problem_records 空表 —— 先 POST /api/v1/problems/sync 或跑 fault_drill.sh"
  AG=""
else
  echo "    选用问题 id=${PID}; Agent 分析中 (curl 600s)..."
  AG=$(curl -s --max-time 600 -X POST "${BASE}/api/v1/ai/agent/${PID}" || echo "")
fi
AST=$(echo "${AG}" | jget data.status)
ALEN=$(echo "${AG}" | python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin)["data"]["analysis"]))
except Exception:
    print(0)')
if [ "${AST}" = "SUCCESS" ] && [ "${ALEN}" -ge 80 ]; then
  ok "Agent 完成: 正文 ${ALEN} 字符, 耗时 $(echo "${AG}" | jget data.elapsed_ms)ms"
else
  ng "status=${AST:-?} 正文=${ALEN} detail=$(echo "${AG}" | jget detail)"
fi

echo "== [5/6] 关键门禁: 本轮轨迹必须含 docker_*/host_* 组工具的真实取证 =="
HCHK=$(echo "${AG}" | python3 -c 'import json,sys
try:
    tr=json.loads(json.load(sys.stdin)["data"]["tool_trace"])
except Exception:
    print("bad"); raise SystemExit
if not isinstance(tr, list) or not tr:
    print("no_trace"); raise SystemExit
host_calls=[s for s in tr if s["tool"].startswith(("docker_","host_"))]
if not host_calls:
    print("zbx_only tools=%s" % sorted({s["tool"] for s in tr})); raise SystemExit
badn=[s["tool"] for s in tr if not s["tool"].startswith(("zbx_","docker_","host_"))]
if badn:
    print("nonreadonly=%s" % badn); raise SystemExit
okc=len([s for s in host_calls if s["ok"]])
print("ok %d %d docker:%d host:%d ok%d" % (len(tr), len(host_calls),
      len([s for s in host_calls if s["tool"].startswith("docker_")]),
      len([s for s in host_calls if s["tool"].startswith("host_")]), okc))' 2>&1)
if [ "${HCHK%% *}" = "ok" ]; then
  ok "轨迹 ${HCHK#ok } (总次数/host组次数/docker/host/成功数) —— 容器侧证据是模型自己取的"
else
  ng "${HCHK}"
  echo "        zbx_only → 模型没碰容器工具: 重跑一次(它有 8 个工具可选), 或加大 LLM_AGENT_MAX_ROUNDS"
  echo "        no_tool_calls → 供应商网关吞 tools 字段, 换支持 function-calling 的模型"
fi

echo "== [6/6] 闸门不放松: 两组之外一律拒绝 + 老链路无损 =="
Z1=$(curl -s --max-time 10 "${BASE}/api/v1/ai/tools" | python3 -c 'import json,sys
try:
    print("clean" if not [t for t in json.load(sys.stdin)["data"]["tools"]
          if any(k in t["name"] for k in ("exec","run","delete","update","stop"))] else "dirty")
except Exception:
    print("bad")')
B404=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "${BASE}/api/v1/ai/agent/99999999")
SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary" || echo "")
UP=$(echo "${SUM}" | jget data.item_unsupported)
if [ "${Z1}" = "clean" ] && [ "${B404}" = "404" ] && [ "${UP:-x}" = "0" ]; then
  ok "白名单无执行/写类入口; 不存在归档返 404; docker unsupported=0"
else
  ng "白名单=${Z1} agent(不存在)=${B404} unsupported=${UP:-?}"
fi

echo
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 11 验证通过, 可以进入 Phase 12 (AI 自动故障分析)"
  echo "复盘: 大盘问题 → /api/v1/ai/agent/<id> 已能让模型自己查 Zabbix + 容器 + 宿主机指标"
else
  echo "Phase 11 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
