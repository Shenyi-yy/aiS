#!/usr/bin/env bash
# =============================================================
# Phase 2 验证脚本
# 运行位置: Ubuntu 宿主机, 在 /opt/zops 下执行: bash scripts/verify_phase2.sh
# 覆盖: systemd 状态 / 10050 监听 / server 容器真实取数 / 10051 发布 / UI 注册 / history 落库
# =============================================================
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PG_USER=${POSTGRES_USER:-zabbix}
PG_DB=${POSTGRES_DB:-zabbix}
HOST_NAME=${HOST_AGENT_NAME:-ubuntu-host}
SERVER_PORT=${ZBX_SERVER_PORT:-10051}
VM_IP=$(hostname -I | awk '{print $1}')

PASS=0; FAIL=0
ok()  { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "============================================================"
echo " Phase 2 验证 | 宿主机 ${VM_IP} | 主机名 ${HOST_NAME}"
echo "============================================================"

echo ""
echo "-- [1] zabbix-agent2 服务状态 (宿主机 systemd) --"
if systemctl is-active --quiet zabbix-agent2; then
  ok "zabbix-agent2 active ($(zabbix_agent2 --version 2>/dev/null | head -n1))"
else
  bad "zabbix-agent2 未运行。执行: sudo systemctl status zabbix-agent2"
fi

echo ""
echo "-- [2] 10050 端口监听 --"
if ss -tln | grep -q ':10050 '; then
  ok "0.0.0.0:10050 已监听"
else
  bad "10050 未监听。执行: sudo journalctl -u zabbix-agent2 -n 50"
fi

echo ""
echo "-- [3] 从 zabbix-server 容器向宿主机 agent2 真实取数 (Passive) --"
if [ ! -f /opt/zops/.env ] && [ ! -f .env ]; then
  bad "未找到 .env, 请在项目根目录运行本脚本"
else
  UNAME=$(docker exec zops-zabbix-server zabbix_get -s "${VM_IP}" -k system.uname 2>&1)
  if [[ "${UNAME}" == Linux* ]]; then
    ok "zabbix_get system.uname -> ${UNAME}"
  else
    bad "取数失败: ${UNAME}"
    echo "        常见原因: Server= 未放行 docker 来源 / UI 未注册不影响本项, 本项只测连通"
  fi
  MEM=$(docker exec zops-zabbix-server zabbix_get -s "${VM_IP}" -k vm.memory.size[available] 2>&1)
  if [[ "${MEM}" =~ ^[0-9]+$ ]]; then
    ok "zabbix_get vm.memory.size[available] -> $(numfmt --to=iec "${MEM}" 2>/dev/null || echo "${MEM} B")"
  else
    bad "内存取数失败: ${MEM}"
  fi
fi

echo ""
echo "-- [4] server 10051 已发布到宿主机 (Active check 通道) --"
if timeout 3 bash -c "echo > /dev/tcp/${VM_IP}/${SERVER_PORT}" 2>/dev/null; then
  ok "${VM_IP}:${SERVER_PORT} 可连通"
else
  bad "${VM_IP}:${SERVER_PORT} 不通。确认已 scp 新 docker-compose.yml 并执行 docker compose up -d zabbix-server"
fi

echo ""
echo "-- [5] Web UI 主机已注册 (直查数据库 hosts 表) --"
HCNT=$(docker exec zops-postgres psql -U "${PG_USER}" -d "${PG_DB}" -tA \
  -c "select count(*) from hosts where host='${HOST_NAME}' and flags=0;" 2>/dev/null || echo 0)
if [ "${HCNT}" -ge 1 ] 2>/dev/null; then
  ok "主机 '${HOST_NAME}' 已注册"
else
  bad "数据库里没有 host='${HOST_NAME}'。请到 UI: Data collection -> Hosts -> Create host (Host name 必须一字不差)"
fi

echo ""
echo "-- [6] 真实监控数据已入库 (history 表) --"
sleep 60
ROWS=$(docker exec zops-postgres psql -U "${PG_USER}" -d "${PG_DB}" -tA -c \
  "select (select count(*) from history h join items i on i.itemid=h.itemid join hosts hs on hs.hostid=i.hostid where hs.host='${HOST_NAME}')
        + (select count(*) from history_uint hu join items i on i.itemid=hu.itemid join hosts hs on hs.hostid=i.hostid where hs.host='${HOST_NAME}');" \
  2>/dev/null || echo 0)
ITEMS=$(docker exec zops-postgres psql -U "${PG_USER}" -d "${PG_DB}" -tA -c \
  "select count(*) from items i join hosts hs on hs.hostid=i.hostid where hs.host='${HOST_NAME}';" 2>/dev/null || echo 0)
if [ "${ITEMS}" -eq 0 ] 2>/dev/null; then
  bad "该主机没有 item (模板没挂上)。编辑主机 -> Templates -> 漏斗搜 'Linux by Zabbix agent' -> Add -> Update"
elif [ "${ROWS}" -gt 0 ] 2>/dev/null; then
  ok "item ${ITEMS} 个, history 已有 ${ROWS} 条真实数据"
else
  bad "item ${ITEMS} 个但 60 秒内无新数据。看 server 日志: docker compose logs zabbix-server | grep -i ubuntu-host | tail"
fi

echo ""
echo "============================================================"
echo " 结果: PASS=${PASS} FAIL=${FAIL}"
if [ "${FAIL}" -eq 0 ]; then
  echo " Phase 2 验证通过, 可以进入 Phase 3"
else
  echo " 存在失败项, 请逐项解决后重跑本脚本"
fi
echo "============================================================"
