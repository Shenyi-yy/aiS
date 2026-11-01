#!/usr/bin/env bash
# =============================================================
# Phase 7: 让宿主机 zabbix-agent2 有权查询 Docker daemon 并自检
# 运行位置: Ubuntu 宿主机, 在 /opt/zops 下执行: bash scripts/setup_docker_monitoring.sh
# 原理: agent2 内建 docker 插件直接读 /var/run/docker.sock (无假数据);
#       socket 属组是 docker, 所以把 agent2 的运行用户 zabbix 加入该组。
# 幂等: 可反复执行。
# =============================================================
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  RUNAS='runuser -u zabbix --'
else
  SUDO="sudo"
  RUNAS='sudo -u zabbix'
fi

echo "==> [1/3] zabbix 用户加入 docker 组"
$SUDO getent group docker >/dev/null || $SUDO groupadd docker
$SUDO usermod -aG docker zabbix
echo "    当前 docker 组成员: $($SUDO getent group docker)"

echo "==> [2/3] 重启 zabbix-agent2 使组成员生效"
$SUDO systemctl restart zabbix-agent2
sleep 2
if systemctl is-active --quiet zabbix-agent2; then
  echo "    zabbix-agent2: active"
else
  echo "    [错误] 启动失败, 最近 30 行日志:"
  $SUDO journalctl -u zabbix-agent2 -n 30 --no-pager | sed 's/^/    /'
  exit 1
fi

echo "==> [3/3] 以 zabbix 身份实测 agent2 Docker 插件 (7.0 合法键 docker.ping)"
# 旧版测 docker.version 并 grep version 是假 PASS: 7.0.31 该键不存在,
# 错误文本 "Unknown metric docker.version" 自带 version。docker.ping 应答 1 才是真凭据。
OUT=$($RUNAS zabbix_agent2 -t docker.ping 2>&1 || true)
echo "${OUT}" | sed 's/^/    /'
if echo "${OUT}" | grep -q '\[s|1\]'; then
  echo "    [OK] agent2 Docker 插件实测应答 1 (有权读 Docker daemon)"
else
  echo "    [错误] docker.ping 未应答 1: 检查 /var/run/docker.sock 权限、zabbix 组是否生效 (id zabbix)"
  exit 1
fi

echo ""
echo "=============================================================="
echo "终端侧就绪。接下来必须做一次 UI 手工挂接 (不挂模板本阶段不会通过):"
echo "  1) 打开 http://<Ubuntu_IP>:8080 → Data collection → Hosts"
echo "     ubuntu-host → Templates → 选 'Docker by Zabbix agent 2' → Update"
echo "  2) Data collection → Discovery rules → 找到该主机的"
echo "     'Docker containers' 发现规则 → Execute now (否则要等默认间隔才发现)"
echo "  3) 等 1-2 分钟后执行: bash scripts/verify_phase7.sh"
echo "=============================================================="
