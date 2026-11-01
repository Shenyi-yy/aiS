#!/usr/bin/env bash
# ============================================================
# ZOPS 一键部署（Phase 1）—— 在 Ubuntu 服务器上执行
#   bash scripts/deploy.sh
# 幂等：可重复执行；已存在的 .env / 数据卷不会被覆盖
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
echo ">> 项目目录: $ROOT"

# ---------- 0. 环境自检 ----------
command -v docker >/dev/null 2>&1 || { echo "!! 未安装 docker，先执行: curl -fsSL https://get.docker.com | sudo sh"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "!! 未安装 docker compose v2 插件"; exit 1; }
docker info >/dev/null 2>&1 || { echo "!! docker 服务未运行，或当前用户无权限: sudo usermod -aG docker \$USER 后重新登录"; exit 1; }

# 清掉可能存在的 CRLF（Windows 上编辑过的文件在 Linux 跑 bash 会报错）
find . -maxdepth 2 -name '*.sh' -exec sed -i 's/\r$//' {} \; 2>/dev/null || true
sed -i 's/\r$//' .env .env.example docker-compose.yml 2>/dev/null || true

# ---------- 1. 生成 .env + 端口占用检查 ----------
if [ ! -f .env ]; then cp .env.example .env; echo ">> 已从 .env.example 生成 .env"; fi
# shellcheck disable=SC1091
set -a; . ./.env; set +a
for p in "${ZABBIX_WEB_PORT:-8080}" "${BACKEND_PORT:-8000}"; do
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"; then
    echo "!! 端口 ${p} 已被占用，请在 .env 里改成其他端口后重跑"; exit 1
  fi
done
echo ">> 端口检查通过: web=${ZABBIX_WEB_PORT:-8080} api=${BACKEND_PORT:-8000}"

# ---------- 2. 密码自动化：仍是示例占位值则生成随机密码 ----------
gen_pw() { openssl rand -base64 24 2>/dev/null | tr -dc 'A-Za-z0-9' | cut -c1-20; }
if grep -q '^ZABBIX_DB_PASSWORD=ChangeMe' .env; then
  DPW=$(gen_pw)
  sed -i "s|^ZABBIX_DB_PASSWORD=.*|ZABBIX_DB_PASSWORD=${DPW}|" .env
  export ZABBIX_DB_PASSWORD="$DPW"
  echo ">> 已生成随机数据库密码（请自行记录，写入 .env 备份处）："
  echo "   ZABBIX_DB_PASSWORD=${DPW}"
fi

# ---------- 3. 目录准备（Phase 1 全用命名卷，无需宿主机权限处理） ----------
mkdir -p docker/alertscripts

# ---------- 4. 构建并启动 ----------
echo ">> docker compose up -d --build （首次约 3-5 分钟：拉镜像 + Zabbix 建表）"
docker compose up -d --build

# ---------- 5. 等待健康 ----------
echo ">> 等待容器健康检查通过（最多 300s）"
for i in $(seq 1 60); do
  bad=0
  for c in zops-postgres zops-zabbix-server zops-backend; do
    st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || echo missing)
    [ "$st" = "healthy" ] || [ "$st" = "running" ] || bad=1
  done
  if [ "$bad" -eq 0 ]; then echo ">> 核心容器全部健康，用时约 $((i*5))s"; break; fi
  sleep 5
  [ "$i" = 60 ] && { echo "!! 超时，请看下面的日志"; docker compose ps; docker logs --tail 30 zops-zabbix-server; exit 1; }
done

# ---------- 6. 验收 ----------
bash scripts/verify_phase1.sh || true

# ---------- 7. 访问信息 ----------
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

============================================================
Phase 1 部署完成
  Zabbix Web : http://${HOST_IP}:${ZABBIX_WEB_PORT:-8080}    (Admin / zabbix, 首次登录强制改密码!)
  FastAPI    : http://${HOST_IP}:${BACKEND_PORT:-8000}/health
  API 文档   : http://${HOST_IP}:${BACKEND_PORT:-8000}/docs
  Zabbix API : http://${HOST_IP}:${ZABBIX_WEB_PORT:-8080}/api_jsonrpc.php (Phase 5 使用)

还差最后一步（页面操作，约 2 分钟）——把 Agent 接到 Server:
  Data collection -> Hosts -> Create host
    Host name : zabbix-agent        (必须与 compose 里 ZBX_HOSTNAME 一致)
    Groups    : Linux servers
    Interface : Add -> Agent, Type=DNS, 值 zabbix-agent, 端口 10050
    Templates : Linux by Zabbix agent
  然后 Monitoring -> Latest data 选该主机，应能看到真实 CPU/内存/网卡数据。

常用排障:
  docker compose ps
  docker logs -f zops-zabbix-server
  bash scripts/verify_phase1.sh
============================================================
EOF
