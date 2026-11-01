"""Phase 13: Docker Engine API 极简执行器 (unix socket, 无 shell)。

这是全系统唯一会改变真实世界的出口, 刻意做得非常小:
- 只有三种方法形态:
    POST /containers/{name}/restart | start | stop
    GET  /containers/{name}/json    (执行后回读状态做核验)
- 传输走 httpx 的 UDS transport 直连 docker.sock, **不经过任何 shell**,
  不存在命令拼接/注入面; name 与 action 已由上层 (exec_catalog +
  services.execution) 双重白名单校验, 这里再兜底校验一次 action 集合。
- 不做 exec/create/rm 等任何"能跑任意命令"或"破坏性"的端点 ——
  代码里没有这条路径, 而不是"有但被禁用"。

诚实兜底: 容器里没挂 socket 时 os.path.exists 为 False, 上层直接拒绝
执行并报 socket 不可用, 不会假装成功。
"""

import logging
import os

import httpx

from app.config import get_settings
from app.services.exec_catalog import ALLOWED_ACTIONS

logger = logging.getLogger("zops.services.docker_exec")


class ExecError(Exception):
    """执行通道层面的失败: socket 不可用 / 白名单校验不过 / 引擎报错。"""


def socket_available() -> bool:
    """执行通道是否存在 (GET /exec/config 如实回显, 不装可用)。"""
    return os.path.exists(get_settings().exec_docker_socket)


def _client(timeout: int | None = None) -> httpx.Client:
    settings = get_settings()
    if not socket_available():
        raise ExecError(
            f"Docker socket 不存在: {settings.exec_docker_socket} "
            "(需 compose 挂载 /var/run/docker.sock)"
        )
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=settings.exec_docker_socket),
        base_url="http://docker",
        timeout=timeout or settings.exec_timeout,
    )


def _wrap_transport(func, *args):
    """把 httpx/OSError 统一折叠成 ExecError。

    首跑实录: backend 容器以非 root 的 zops 用户运行, socket 属
    root:docker —— 没补组时 connect 直接 PermissionError。这类异常如果
    裸抛会变成 HTTP 500, 审批证据链全断; 折叠后 approve 能把原因写进
    单据 error 并落 FAILED, 排查一眼定位。
    """
    try:
        return func(*args)
    except (httpx.HTTPError, OSError) as exc:
        msg = f"{type(exc).__name__}: {exc}"
        if "Permission" in msg or "EACCES" in msg or "denied" in msg.lower():
            msg += " —— 容器内用户无权访问 docker.sock (检查 compose group_add 的 DOCKER_GID)"
        raise ExecError(f"Docker 引擎通信失败: {msg[:300]}") from exc


def container_state(name: str) -> str:
    """读回容器运行状态 (running/exited/...), 用于执行后核验。"""
    def call():
        with _client() as cli:
            return cli.get(f"/containers/{name}/json")
    resp = _wrap_transport(call)
    if resp.status_code == 404:
        raise ExecError(f"容器 {name} 不存在")
    if resp.status_code >= 400:
        raise ExecError(f"查询容器 {name} 失败: HTTP {resp.status_code}")
    return str(resp.json().get("State", {}).get("Status", "unknown"))


def run_container_action(action: str, name: str) -> dict[str, object]:
    """执行一个白名单容器动作并回读状态。

    返回 {"ok": True, "state": 执行后状态, "http_status": 引擎返回码}。
    引擎 2xx/304 都算受理成功 (304 = 已在目标状态, 对 restart/start/stop
    而言同样是"达到了期望"), 其余状态码抛 ExecError 并带引擎原文。
    """
    if action not in ALLOWED_ACTIONS:
        # 双保险: 正常路径不可能到这里, 到了说明上面某层校验被绕过
        raise ExecError(f"动作 {action!r} 不在固定集合 {sorted(ALLOWED_ACTIONS)}")

    def call():
        with _client() as cli:
            return cli.post(f"/containers/{name}/{action}")

    resp = _wrap_transport(call)
    if resp.status_code >= 400:
        detail = (resp.text or "")[:300]
        raise ExecError(f"docker {action} {name}: HTTP {resp.status_code} {detail}")
    state = container_state(name)
    logger.info("已执行 docker %s %s (HTTP %s → 状态 %s)",
                action, name, resp.status_code, state)
    return {
        "ok": True,
        "state": state,
        "http_status": resp.status_code,
    }
