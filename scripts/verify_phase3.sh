#!/usr/bin/env bash
# =============================================================
# Phase 3 验证脚本
# 运行位置: Ubuntu 宿主机, 在 /opt/zops 下执行: bash scripts/verify_phase3.sh
# 前置: 已在 UI 建好 3 个 ZOPS: 触发器, 并已跑过 scripts/trigger_drill.sh
# =============================================================
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PG_USER=${POSTGRES_USER:-zabbix}
PG_DB=${POSTGRES_DB:-zabbix}
HOST_NAME=${HOST_AGENT_NAME:-ubuntu-host}

PASS=0; FAIL=0
ok()  { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
q()   { docker exec zops-postgres psql -U "${PG_USER}" -d "${PG_DB}" -tA -c "$1" 2>/dev/null || echo ''; }

echo "============================================================"
echo " Phase 3 验证 | Trigger + Problems | 主机 ${HOST_NAME}"
echo "============================================================"

echo ""
echo "-- [1] 3 个 ZOPS 自定义触发器已创建且为启用状态 --"
TRIG=$(q "select count(*) from triggers where description like 'ZOPS:%' and status = 0;")
if [ "${TRIG:-0}" -ge 3 ] 2>/dev/null; then
  ok "启用中的 ZOPS 触发器: ${TRIG} 个"
  q "select '        ' || description from triggers where description like 'ZOPS:%' and status = 0;"
else
  bad "只找到 ${TRIG:-0}/3 个。检查 UI 触发器是否创建、名称是否以 'ZOPS:' 开头、Status 是否 Enabled"
fi

echo ""
echo "-- [2] 触发器表达式已绑定到 ${HOST_NAME} 的真实 item --"
BIND=$(q "select count(distinct t.triggerid)
            from triggers t
            join functions f on f.triggerid = t.triggerid
            join items i on i.itemid = f.itemid
            join hosts hs on hs.hostid = i.hostid
           where t.description like 'ZOPS:%' and hs.host = '${HOST_NAME}';")
if [ "${BIND:-0}" -ge 3 ] 2>/dev/null; then
  ok "${BIND} 个触发器已挂到宿主机 item"
else
  bad "仅 ${BIND:-0} 个触发器绑到该主机 item。表达式里主机名必须是 /${HOST_NAME}/... 且 Expression 状态为 Enabled"
fi

echo ""
echo "-- [3] 真实问题已产生 (problem 表, 需先跑 trigger_drill.sh) --"
PROB=$(q "select count(*) from problem p join triggers t on t.triggerid = p.objectid
           where p.source = 0 and p.object = 0 and t.description like 'ZOPS:%';")
if [ "${PROB:-0}" -ge 1 ] 2>/dev/null; then
  ok "ZOPS 问题记录数: ${PROB}"
  q "select '        ' || t.description || case when p.r_eventid is null then ' [ACTIVE]' else ' [RECOVERED]' end || ' sev=' || p.severity
       from problem p join triggers t on t.triggerid = p.objectid
      where p.source = 0 and p.object = 0 and t.description like 'ZOPS:%' order by p.clock desc limit 5;"
else
  bad "没有问题记录。先执行: bash scripts/trigger_drill.sh 360 (压满 CPU 等表达式 avg 5m 超限)"
fi

echo ""
echo "-- [4] 事件流水完整 (7.0 事件表为 events 复数; 经 trigger 关联) --"
EV=$(q "select count(*) from events e join triggers t on e.objectid::text = t.triggerid::text
         where e.source = 0 and e.object = 0 and t.description like 'ZOPS:%';")
if [ "${EV:-0}" -ge 1 ] 2>/dev/null; then
  ok "ZOPS 相关事件数: ${EV} (含问题产生/恢复)"
else
  bad "events 表无 ZOPS 事件, 与 [3] 矛盾, 说明触发器从未真正评估过"
fi

echo ""
echo "============================================================"
echo " 结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo " Phase 3 验证通过, 可以进入 Phase 4"
else
  echo " 存在失败项, 请逐项解决后重跑本脚本"
fi
echo "============================================================"
