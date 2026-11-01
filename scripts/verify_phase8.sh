#!/usr/bin/env bash
# =============================================================
# Phase 8 验证脚本 v2: 故障演练闭环的证据核验 (全程只读, 不制造/不恢复故障)
# 运行位置: Ubuntu 宿主机  cd /opt/zops && bash scripts/verify_phase8.sh
# 通关标准: PASS=5 FAIL=0
# 前置条件: 已跑 bash scripts/fault_drill.sh 且走到 [7/7] 演练完成
# 对应新设计: 触发器绑模板 LLD 项 docker.container_info.state.running["/zops-drill"]
#             (问题式 =0, 恢复式 =1), 不再有自建 ZOPS item。
# =============================================================
set -u
cd "$(dirname "$0")/.."

BASE="http://127.0.0.1:${BACKEND_PORT:-8000}"
PG="zops-postgres"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
VM_IP=$(hostname -I | awk '{print $1}')
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

DRILL_KEY='docker.container_info.state.running["/zops-drill"]'

echo "== [1/5] 故障已解除: zops-drill 运行中且 Zabbix 采集值回到 1 =="
# 7.0 实证: items 表没有 lastvalue 列, 真实数值只在 history_uint(history_str/history)
RUN=$(${SUDO} docker inspect -f '{{.State.Running}}' zops-drill 2>/dev/null || echo missing)
LV=$(${SUDO} docker exec "${PG}" psql -U zabbix -d zabbix -tA -c \
  "select value from history_uint
   where itemid=(select i.itemid from items i join hosts h on h.hostid=i.hostid
                 where h.name='ubuntu-host' and i.key_=\$\$${DRILL_KEY}\$\$)
   order by clock desc limit 1;" 2>/dev/null | tr -d '[:space:]')
if [ "${RUN}" = "true" ] && [ "${LV}" = "1" ]; then
  ok "zops-drill Running=true, history_uint 最新值=1 (停止态已自愈)"
else
  ng "Running=${RUN} 最新采集值=${LV:-查无此项} —— 先跑 fault_drill.sh 走完整轮"
fi

echo "== [2/5] 触发器存在且绑定的是模板项 (只建触发器, 无自建 item) =="
TRG=$(${SUDO} docker exec "${PG}" psql -U zabbix -d zabbix -tA -c \
  "select count(*) from triggers t join functions f on f.triggerid=t.triggerid
   join items i on i.itemid=f.itemid
   where t.description like 'ZOPS演练%' and i.key_=\$\$${DRILL_KEY}\$\$;" 2>/dev/null | tr -d '[:space:]')
CUSTOM=$(${SUDO} docker exec "${PG}" psql -U zabbix -d zabbix -tA -c \
  "select count(*) from items i join hosts h on h.hostid=i.hostid
   where h.name='ubuntu-host' and i.key_ like 'docker.container.get%';" 2>/dev/null | tr -d '[:space:]')
if [ "${TRG:-0}" -ge 1 ] && [ "${CUSTOM:-0}" = "0" ]; then
  ok "ZOPS演练触发器 ${TRG} 个, 绑定的正是模板项; 旧设计的自建 item 残留 0 条"
else
  ng "触发器=${TRG:-?} 自建item残留=${CUSTOM:-?} (应 >=1 且 =0)"
fi

echo "== [3/5] Zabbix events 表交叉核对: 每条 PROBLEM 都有恢复指针 =="
# 7.0 实证: events 表没有 r_eventid 列, 恢复指针在 event_recovery 表
EVT=$(${SUDO} docker exec "${PG}" psql -U zabbix -d zabbix -tA -c \
  "select count(*) filter (where e.value=1) || ' ' ||
          count(*) filter (where e.value=1 and er.r_eventid is not null)
   from events e
   join triggers t on t.triggerid = e.objectid
   left join event_recovery er on er.eventid = e.eventid
   where t.description like 'ZOPS演练%';" 2>/dev/null)
set -- ${EVT:-0 0}
P_CNT=${1:-0}; R_CNT=${2:-0}
if [ "${P_CNT}" -ge 1 ] && [ "${R_CNT}" = "${P_CNT}" ]; then
  ok "PROBLEM 事件 ${P_CNT} 条, 其中 ${R_CNT} 条 r_eventid 已指向恢复事件 (闭环)"
else
  ng "PROBLEM=${P_CNT} 已恢复=${R_CNT} —— 触发器没触发或没恢复, 看 UI Problems"
fi

echo "== [4/5] 后端归档: RECOVERED 且 recovered_at 晚于 started_at, 无 ACTIVE 残留 =="
read -r REC_CNT ACT_CNT TC < <(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=500" | python3 -c 'import json,sys
try:
    items = json.load(sys.stdin)["data"]["items"]
except Exception:
    items = []
rows = [it for it in items if "zops-drill" in it["trigger_name"]]
rec = [it for it in rows if it["status"] == "RECOVERED" and it.get("recovered_at")]
act = sum(1 for it in rows if it["status"] == "ACTIVE")
tcs = ["ok" if it["recovered_at"] > it["started_at"] else "bad" for it in rec]
print(len(rec), act, (tcs[0] if tcs else "none"))')
if [ "${REC_CNT:-0}" -ge 1 ] && [ "${ACT_CNT:-0}" -eq 0 ] && [ "${TC:-none}" = "ok" ]; then
  ok "归档 ${REC_CNT} 条已恢复演练问题, 无 ACTIVE 残留, 时间序正确 (${TC})"
else
  ng "RECOVERED=${REC_CNT:-?} ACTIVE=${ACT_CNT:-?} 时间=${TC:-?}"
fi

echo "== [5/5] 演练后全链路仍健康: docker 收数零错误 + 归档幂等 =="
SUM=$(curl -sf --max-time 20 "${BASE}/api/v1/docker/summary" || echo "")
UP=$(echo "${SUM}" | jget data.item_unsupported)
ER=$(echo "${SUM}" | jget data.item_with_error)
TR=$(echo "${SUM}" | jget data.trigger_total)
curl -s -X POST "${BASE}/api/v1/problems/sync" >/dev/null
T1=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=1" | jget data.total)
curl -s -X POST "${BASE}/api/v1/problems/sync" >/dev/null
T2=$(curl -s --max-time 10 "${BASE}/api/v1/problems?limit=1" | jget data.total)
if [ "${UP:-x}" = "0" ] && [ "${ER:-x}" = "0" ] && [ "${TR:-0}" -ge 1 ] && \
   [ -n "${T1}" ] && [ "${T1}" = "${T2}" ]; then
  ok "docker 项 unsupported=0 error=0 触发器=${TR}; 归档 total=${T1} 两次同步稳定"
else
  ng "unsupported=${UP:-?} error=${ER:-?} triggers=${TR:-?} total ${T1:-?}/${T2:-?}"
fi

echo
echo "============================================================"
echo "结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo "Phase 8 验证通过, 可以进入 Phase 9 (AI Agent 接入)"
  echo "复盘: 大盘 http://${VM_IP}:8000/ 问题列表可看到该演练的 ACTIVE->RECOVERED 历史"
else
  echo "Phase 8 未通过: 按上面 [FAIL] 行提示排查后重跑本脚本"
fi
