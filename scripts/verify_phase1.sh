#!/usr/bin/env bash
# ============================================================
# ZOPS Phase 1 一键验证 —— 在 Ubuntu 服务器上执行
#   bash scripts/verify_phase1.sh
# 检查: 容器状态 / HTTP 接口 / Zabbix 是否已注册 agent 主机
# ============================================================
set -uo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }

WEB_PORT="${ZABBIX_WEB_PORT:-8080}"
API_PORT="${BACKEND_PORT:-8000}"
PG_USER="${POSTGRES_USER:-zabbix}"
PG_DB="${POSTGRES_DB:-zabbix}"
EXPECTED_HOST="${ZBX_AGENT_HOSTNAME:-zabbix-agent}"

pass=0; fail=0
ok()   { echo "  [PASS] $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }

echo "== 1. 容器状态 =="
for c in zops-postgres zops-zabbix-server zops-zabbix-web zops-zabbix-agent zops-backend; do
  state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null || echo missing)
  if [ "$state" = "running" ]; then ok "$c: running (health=$health)"; else bad "$c: $state"; fi
done

echo "== 2. HTTP 接口 =="
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${WEB_PORT}/index.php")
[ "$code" = "200" ] && ok "Zabbix Web http://127.0.0.1:${WEB_PORT} -> $code" || bad "Zabbix Web -> $code"

body=$(curl -s "http://127.0.0.1:${API_PORT}/health")
echo "$body" | grep -q '"status":"ok"' && ok "FastAPI /health -> $body" || bad "FastAPI /health -> $body"

echo "== 3. Agent 主机是否已注册到 Zabbix =="
hosts=$(docker exec zops-postgres psql -U "$PG_USER" -d "$PG_DB" -tA \
  -c "select host from hosts where status=0 and flags=0 and host <> '';" 2>/dev/null || true)
if echo "$hosts" | grep -q "^${EXPECTED_HOST}$"; then
  ok "已注册主机: $(echo $hosts | tr '\n' ' ')"
else
  bad "hosts 表里没有 ${EXPECTED_HOST} —— 请先在 Web UI 添加主机（见 docs/phase-1.md 第 4 步）后重跑本脚本"
fi

echo "== 4. Agent 数据真实上报检查（需已注册主机, 否则跳过） =="
if echo "$hosts" | grep -q "^${EXPECTED_HOST}$"; then
  # Zabbix 7.0: items 表无 lastvalue/error 列; 真实数值在 history*(按 value_type 分表)
  cnt=$(docker exec zops-postgres psql -U "$PG_USER" -d "$PG_DB" -tA \
    -c "select (select count(*) from history h join items i on i.itemid=h.itemid join hosts hs on hs.hostid=i.hostid where hs.host='${EXPECTED_HOST}') + (select count(*) from history_uint hu join items i on i.itemid=hu.itemid join hosts hs on hs.hostid=i.hostid where hs.host='${EXPECTED_HOST}');" || echo 0)
  if [ "${cnt:-0}" -gt 0 ]; then
    ok "${EXPECTED_HOST} 已写入 ${cnt} 条真实数据 (history/history_uint)"
  else
    bad "${EXPECTED_HOST} 已注册但还没有取到数据 —— 等 1-2 分钟重跑; 仍失败查 docs/phase-1.md 排障表"
  fi
else
  echo "  [SKIP] 主机未注册"
fi

echo
echo "结果: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ] && echo "Phase 1 验证通过，可以进入 Phase 2" || echo "还有项没通过，按上面 [FAIL] 行排查"
