#!/usr/bin/env bash
# =============================================================
# Phase 2: 在 Ubuntu 宿主机(不是容器)原生安装并配置 zabbix-agent2
# 运行位置: Ubuntu 宿主机, 在 /opt/zops 下执行: bash scripts/setup_host_agent.sh
# 幂等: 已安装则跳过安装, 只刷新配置; 可反复执行
# =============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

HOST_NAME=${HOST_AGENT_NAME:-ubuntu-host}
SERVER_PORT=${ZBX_SERVER_PORT:-10051}
CONF=/etc/zabbix/zabbix_agent2.conf

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

echo "==> [1/5] 环境检测"
. /etc/os-release
echo "    系统: ${PRETTY_NAME:-$ID $VERSION_ID}"
case "${VERSION_ID:-}" in
  22.04|24.04) ;;
  *) echo "    [错误] 本脚本仅支持 Ubuntu 22.04 / 24.04 (Zabbix 7.0 官方仓库覆盖版本)"; exit 1 ;;
esac

# 宿主机主 IP: agent2 监听 0.0.0.0:10050, UI 里主机接口要填的就是它
VM_IP=$(hostname -I | awk '{print $1}')
if [ -z "${VM_IP}" ]; then echo "    [错误] 未能检测到宿主机 IP (hostname -I 为空)"; exit 1; fi
echo "    宿主机 IP: ${VM_IP}"

# zabbix-server 容器在宿主机网络里的网段/网关: Passive check 请求到达 agent2 时
# 源地址会被 docker SNAT 成网关 IP, 所以 Server= 必须放行这些来源
NET_INFO=$(docker network inspect zops_zops-net \
  --format '{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}' 2>/dev/null || true)
SUBNET=$(echo "${NET_INFO}" | awk '{print $1}')
GATEWAY=$(echo "${NET_INFO}" | awk '{print $2}')
# Server= 不允许重复地址: 网关 IP 本身就在网段 CIDR 内, 有 SUBNET 就不要再加 GATEWAY
SERVER_ALLOW="127.0.0.1"
add_allow() {
  case ",${SERVER_ALLOW}," in
    *",$1,"*) ;;
    *) [ -n "$1" ] && SERVER_ALLOW="${SERVER_ALLOW},$1" ;;
  esac
}
add_allow "${VM_IP}"
if [ -n "${SUBNET}" ]; then add_allow "${SUBNET}"; else add_allow "${GATEWAY}"; fi
echo "    Server= 放行来源: ${SERVER_ALLOW}"

echo "==> [2/5] 安装 zabbix-agent2 (已安装则跳过)"
if command -v zabbix_agent2 >/dev/null 2>&1; then
  echo "    已存在: $(zabbix_agent2 --version | head -n1), 跳过安装"
else
  REL_DEB="/tmp/zabbix-release_7.0.deb"
  wget -q -O "${REL_DEB}" \
    "https://repo.zabbix.com/zabbix/7.0/ubuntu/pool/main/z/zabbix-release/zabbix-release_latest_7.0+ubuntu${VERSION_ID}_all.deb"
  DEBIAN_FRONTEND=noninteractive $SUDO dpkg -i "${REL_DEB}"
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get update -y
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y zabbix-agent2
  echo "    安装完成: $(zabbix_agent2 --version | head -n1)"
fi

echo "==> [3/5] 写入配置 ${CONF}"
# 幂等改写: 先删除该 key 的所有旧行(含被注释的示例行, 防止展开成重复配置), 再在文件末尾追加唯一一行
set_conf() {
  local key="$1" val="$2"
  $SUDO sed -i -E "/^[#[:space:]]*${key}=/d" "${CONF}"
  echo "${key}=${val}" | $SUDO tee -a "${CONF}" >/dev/null
}
set_conf Server       "${SERVER_ALLOW}"        # Passive: 允许谁来取数 (逗号分隔, 支持 CIDR)
set_conf ServerActive "${VM_IP}:${SERVER_PORT}" # Active: agent 反向连 server (宿主机发布端口), 不能带 CIDR
set_conf Hostname     "${HOST_NAME}"           # 必须与 Web UI Host name 一字不差
# sed -i 重写文件后修正属组/权限, 保证 systemd 的 zabbix 用户可读 (否则服务直接启动失败)
$SUDO chown root:zabbix "${CONF}" 2>/dev/null || true
$SUDO chmod 640 "${CONF}" 2>/dev/null || true
$SUDO grep -E "^(Server|ServerActive|Hostname)=" "${CONF}" | sed 's/^/    /'

echo "==> [4/5] 启动服务 (systemd)"
# 先看端口有没有被老包 zabbix-agent(v1) 占用
if ss -tln | grep -q ':10050 ' && ! systemctl is-active --quiet zabbix-agent2; then
  echo "    [提示] 10050 已被其他进程占用, 占用者:"
  ss -tlnp | grep ':10050 ' | sed 's/^/    /'
  echo "    若是老版 zabbix-agent: sudo systemctl disable --now zabbix-agent && sudo apt-get remove -y zabbix-agent"
fi
$SUDO systemctl enable --now zabbix-agent2 2>/dev/null || true
$SUDO systemctl restart zabbix-agent2 || true
sleep 2
if systemctl is-active --quiet zabbix-agent2; then
  echo "    zabbix-agent2: active"
else
  echo "    [错误] 启动失败, 最近 30 行日志:"
  $SUDO journalctl -u zabbix-agent2 -n 30 --no-pager | sed 's/^/    /'
  exit 1
fi

echo "==> [5/5] 端口自检"
if ss -tln | grep -q ':10050 '; then
  echo "    10050 已监听"
else
  echo "    [错误] 10050 未监听, 请查看: sudo journalctl -u zabbix-agent2 -n 50"
  exit 1
fi

echo ""
echo "=============================================================="
echo "宿主机 agent2 就绪。接下来去 Zabbix Web UI (http://${VM_IP}:8080)"
echo "添加主机: Host name = ${HOST_NAME}"
echo "         接口 Type = IP, 值 = ${VM_IP}, 端口 10050"
echo "         模板 = Linux by Zabbix agent"
echo "完成后回到终端执行: bash scripts/verify_phase2.sh"
echo "=============================================================="
