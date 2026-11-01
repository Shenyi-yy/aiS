# 部署说明（Phase 1）

要求：Ubuntu 22.04/24.04，Docker Engine 24+ 与 Docker Compose v2，内存 ≥ 2G（推荐 4G，Zabbix + PostgreSQL + FastAPI 全跑约 1.5G），磁盘 ≥ 20G。

## 一、从 Windows 把项目传到 Ubuntu

**Windows PowerShell**（把 用户名 / Ubuntu_IP 换成实际值）：

```powershell
scp -r C:\Users\YANGSHIYI\Desktop\aiS\zops 用户名@Ubuntu_IP:/opt/
```

> 用 MobaXterm 直接拖拽上传效果一样。Git 方式（推荐后续阶段使用）：
> Windows `git push` → Ubuntu `git clone / git pull`。
> 注意：**永远不要**把 Windows 本地的 `.env` 传上去，`.env` 只在 Ubuntu 上生成。

## 二、Ubuntu 一键部署

```bash
cd /opt/zops && bash scripts/deploy.sh
```

脚本自动完成：docker 自检 → 清 CRLF → 生成 `.env`（占位密码自动替换为随机密码并打印）→ 端口占用检查 → `docker compose up -d --build` → 等待健康检查（最多 300s）→ 执行 `scripts/verify_phase1.sh` → 打印访问地址。

**成功标志**：末尾输出 `Phase 1 验证通过，可以进入 Phase 2`，并列出 Zabbix Web / FastAPI 地址。

## 三、部署后必须手工做一次（2 分钟）

Zabbix 里注册 Agent 主机（Passive 模式靠 Host name 与 agent 的 Hostname 匹配）：

`Data collection → Hosts → Create host`
- Host name：`zabbix-agent`（必须与 compose 里 `ZBX_HOSTNAME` 一致）
- Groups：Linux servers
- Interface：Add → Agent，Type=DNS，值 `zabbix-agent`，端口 10050
- Templates：`Linux by Zabbix agent` → Add

首次登录 Web（Admin / zabbix）会**强制修改密码**，改后记牢——Phase 5 要填进 `.env` 的 `ZABBIX_PASSWORD`。

完成后重跑一次 `bash scripts/verify_phase1.sh`，第 3、4 步应变为 PASS（即 Agent 真实数据已进入数据库）。

## 四、日常命令

```bash
docker compose ps                     # 看状态
docker compose logs -f zops-zabbix-server 2>/dev/null || docker compose logs -f zabbix-server
docker compose restart zabbix-web     # 单独重启某个组件（按 compose 服务名）
docker compose down                   # 停止（保留数据卷）
docker compose down -v                # 停止并删除数据库卷（重做初始化，会丢监控数据）
```

## 五、生产环境补充项（MVP 不做，面试时说明边界）

- Zabbix Web 前置 Nginx 做 HTTPS
- 数据库密码从 Vault / 环境注入，不落 `.env` 明文
- Housekeeper 保留策略 + 历史表分区
- 宿主机原生安装 zabbix-agent2 监控 Linux 真机（Phase 2）
