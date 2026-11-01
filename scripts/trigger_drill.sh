#!/usr/bin/env bash
# =============================================================
# Phase 3 演练: 在宿主机制造真实 CPU 压力, 触发 ZOPS 自定义触发器
# 运行位置: Ubuntu 宿主机, 在 /opt/zops 下执行: bash scripts/trigger_drill.sh [秒数,默认360]
# 原理: 用 nproc 个 `yes > /dev/null` 进程压满 CPU (真实负载, 非 fake 数据)
#       每 15 秒打印负载 + 数据库中 ZOPS 问题状态, Ctrl+C 可随时停止压测
# =============================================================
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PG_USER=${POSTGRES_USER:-zabbix}
PG_DB=${POSTGRES_DB:-zabbix}
DURATION=${1:-360}
WORKERS=$(nproc)

echo "==> [1/2] 启动 ${WORKERS} 个 CPU 压力进程, 持续 ${DURATION}s (Ctrl+C 提前结束压测)"
pids=()
for _ in $(seq 1 "${WORKERS}"); do yes >/dev/null & pids+=($!); done
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "==> [2/2] 监控触发状态 (avg 5m 表达式需要几分钟积累, 耐心等)"
PROB_SQL="select coalesce(string_agg(t.description || case when p.r_eventid is null then ' [ACTIVE]' else ' [RECOVERED]' end, ' | '), '-')
from problem p join triggers t on t.triggerid = p.objectid
where p.source = 0 and p.object = 0 and t.description like 'ZOPS:%';"

start=$(date +%s)
while :; do
  el=$(( $(date +%s) - start ))
  [ "${el}" -ge "${DURATION}" ] && break
  load=$(awk '{print $1", "$2", "$3}' /proc/loadavg)
  prob=$(docker exec zops-postgres psql -U "${PG_USER}" -d "${PG_DB}" -tA -c "${PROB_SQL}" 2>/dev/null || echo '-')
  echo "  [${el}s] loadavg=${load}  问题: ${prob}"
  sleep 15
done

echo ""
echo "==> 压测结束, CPU 将回落。若触发器配了恢复, 问题稍后会自动变为 RECOVERED。"
echo "    验证: bash scripts/verify_phase3.sh"
