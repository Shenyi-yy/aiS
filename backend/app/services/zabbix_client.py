"""Zabbix API 客户端 —— 全后端唯一直接调用 Zabbix API 的模块。

项目约定: Zabbix API 调用不得散落到各个文件。路由/同步任务只许调用本模块
的语义方法; 新增 API 能力一律在这里加封装。

设计要点:
- 同步 httpx.Client: FastAPI 对 sync 路由自动走线程池, 后台周期任务用
  asyncio.to_thread 调用, 全链路不阻塞事件循环, 也不必到处 async。
- auth token 缓存在客户端内部; 会话失效自动重登并重试一次。
- 凭据缺失/错误统一抛 ZabbixError, 上层决定怎么呈现, 不泄漏密码。
"""

import itertools
import logging
import threading
from typing import Any

import httpx

logger = logging.getLogger("zops.services.zabbix_client")


class ZabbixError(RuntimeError):
    """Zabbix API 调用失败 (网络 / 认证 / 服务端错误统一归一到这里)。"""


class ZabbixClient:
    """线程安全的 Zabbix JSON-RPC 客户端 (单 token 会话)。"""

    def __init__(
        self,
        api_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
    ) -> None:
        self._api_url = api_url
        self._username = username
        self._password = password
        self._token: str | None = None
        self._ids = itertools.count(1)
        self._http = httpx.Client(timeout=timeout)
        self._login_lock = threading.Lock()

    # ---------- 底层 ----------

    def _call(
        self,
        method: str,
        params: dict[str, Any] | list[Any] | None,
        need_auth: bool,
    ) -> Any:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else [],
            "id": next(self._ids),
        }
        if need_auth:
            if self._token is None:
                self.login()
            body["auth"] = self._token
        try:
            resp = self._http.post(self._api_url, json=body)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ZabbixError(f"Zabbix API 网络/解析失败 ({method}): {exc}") from exc

        if "error" in payload:
            err = payload["error"]
            data = str(err.get("data", ""))
            lowered = data.lower()
            if self._token is not None and (
                "not authorised" in lowered or "session terminated" in lowered
            ):
                logger.info("Zabbix 会话失效, 重新登录后重试 %s", method)
                self._token = None
                return self._call(method, params, need_auth)
            raise ZabbixError(
                f"Zabbix API 返回错误 ({method}): {err.get('message')} / {data}"
            )
        return payload.get("result")

    def login(self) -> str:
        """user.login 换取 session token (Zabbix 7.0 返回 JWT 字符串)。"""
        if not self._password:
            raise ZabbixError(
                "ZABBIX_PASSWORD 未配置: 请在 Ubuntu 侧编辑 /opt/zops/.env 填入 Web 登录密码"
                " (真实密码不要写进代码/聊天/仓库)"
            )
        with self._login_lock:
            if self._token is not None:
                return self._token
            token = self._call(
                "user.login",
                {"username": self._username, "password": self._password},
                need_auth=False,
            )
            if not isinstance(token, str) or not token:
                raise ZabbixError("user.login 未返回有效 token (用户名或密码错误?)")
            self._token = token
            logger.info("Zabbix API 登录成功 (user=%s)", self._username)
            return token

    # ---------- 语义方法 (路由层只调这些) ----------

    def health(self) -> str:
        """登录 + apiinfo.version, 返回版本号; 失败抛 ZabbixError。"""
        self.login()
        version = self._call("apiinfo.version", None, need_auth=False)
        if not isinstance(version, str):
            raise ZabbixError(f"apiinfo.version 返回异常: {version!r}")
        return version

    def fetch_problem_events(self, time_from: int, limit: int = 500) -> list[dict]:
        """拉取指定时间之后的 PROBLEM 事件 (source=0/object=0/value=1)。"""
        result = self._call(
            "event.get",
            {
                "source": 0,
                "object": 0,
                "value": 1,
                "time_from": time_from,
                "output": "extend",
                "selectHosts": ["host", "name"],
                "sortfield": "eventid",
                "sortorder": "DESC",
                "limit": limit,
            },
            need_auth=True,
        )
        return result or []

    def fetch_event_detail(self, eventid: str) -> dict:
        """Phase 9: 按 eventid 回查单个事件的完整原始字段 (给 AI 当证据)。

        归档表只存了六七个字段, 分析时要把 Zabbix 侧的原始事件拉回来对质;
        eventids 参数是字符串数组 (7.0 实测), 查不到抛 ZabbixError 语义化异常。
        """
        result = self._call(
            "event.get",
            {
                "eventids": [str(eventid)],
                "output": "extend",
                "selectHosts": ["hostid", "host", "name"],
            },
            need_auth=True,
        ) or []
        if not result:
            raise ZabbixError(
                f"event.get 查无此事件 (eventid={eventid}) —— 事件可能已超出保留期"
            )
        return result[0]

    def fetch_host_items(
        self, host_name: str, key_search: str = "", limit: int = 30
    ) -> list[dict]:
        """Phase 10: 查指定主机的监控项最新值 (AI 工具专用, 只读)。

        item.get 的 host 参数按 Host name 精确过滤; lastvalue 只在 API
        返回里有 (DB items 表没有该列 —— 7.0 铁律), 所以走 API 不走 SQL。
        """
        params: dict[str, Any] = {
            "output": ["itemid", "name", "key_", "lastvalue", "lastclock", "state", "error"],
            "host": host_name,
            "sortfield": ["name"],
            "limit": limit,
        }
        if key_search:
            params["search"] = {"key_": key_search}
        result = self._call("item.get", params, need_auth=True)
        return result or []

    def fetch_active_triggers(self, host_name: str = "", limit: int = 50) -> list[dict]:
        """Phase 10: 当前处于 PROBLEM 状态的触发器 (trigger.get value=1)。

        7.0 陷阱复用法则: trigger 的当前状态在 value 字段 (0=OK, 1=PROBLEM),
        status 只是启用标志 —— filter 用 value 不用 status。
        """
        params: dict[str, Any] = {
            "output": ["triggerid", "description", "priority", "lastchange"],
            "filter": {"value": [1]},
            "expandDescription": True,
            "selectHosts": ["host", "name"],
            "sortfield": ["priority", "lastchange"],
            "sortorder": "DESC",
            "limit": limit,
        }
        if host_name:
            params["host"] = [host_name]
        result = self._call("trigger.get", params, need_auth=True)
        return result or []

    def resolve_recovery_clocks(self, problem_events: list[dict]) -> dict[str, int]:
        """从 PROBLEM 事件解析恢复时间, 返回 {problem_eventid: 恢复时间戳}。

        7.0 陷阱: events 表没有 related_eventid 字段——问题事件的 r_eventid
        才指向其恢复事件 ("0" = 尚未恢复), 恢复时间要拿 r_eventid 回查 clock。
        """
        pairs = {
            str(e["eventid"]): str(e.get("r_eventid") or "0")
            for e in problem_events
            if str(e.get("r_eventid") or "0") != "0"
        }
        if not pairs:
            return {}
        recovery_ids = sorted(set(pairs.values()))
        records = self._call(
            "event.get",
            {"eventids": recovery_ids, "output": ["eventid", "clock"]},
            need_auth=True,
        ) or []
        clock_by_id = {str(r["eventid"]): int(r["clock"]) for r in records}
        return {
            problem_id: clock_by_id[rec_id]
            for problem_id, rec_id in pairs.items()
            if rec_id in clock_by_id
        }

    def fetch_hosts(self) -> list[dict]:
        """受监控主机列表 (host.get, status=0 即 MONITORED)。"""
        result = self._call(
            "host.get",
            {
                "output": ["hostid", "host", "name", "status"],
                "filter": {"status": [0]},
                "sortfield": "name",
            },
            need_auth=True,
        )
        return result or []

    def fetch_items_by_key(self, item_key: str, limit: int = 20) -> list[dict]:
        """按 key 模糊查 item, 返回 Zabbix 原始字段 (lastvalue/lastclock 等)。

        用于诊断 item 是否真的在收数 (如 Phase 3 遗留的磁盘手动 item)。
        """
        result = self._call(
            "item.get",
            {
                "output": "extend",
                "search": {"key_": item_key},
                "selectHosts": ["host", "name"],
                "limit": limit,
            },
            need_auth=True,
        )
        return result or []

    def fetch_monitor_items(self, key_prefix: str = "docker.", limit: int = 1000) -> list[dict]:
        """Phase 7: 查某类监控项 (如 Docker 模板 LLD 发现的 docker.* item)。

        注意: DB items 表没有 lastvalue/error 列, 但 API item.get 返回里有
        (Phase 5 已实证) —— 诊断一律走 API 不走 SQL。
        7.0 实测: 新版 Docker 无 KernelMemory 字段, 模板的 docker.kernel_mem*
        探测项永远 unsupported, 运维侧禁用后这里必须按 status=0 只统计"启用的
        监控项", 否则已禁用项会一直把 error/unsupported 计数顶高。
        Phase 11 实证教训: 默认 limit 曾为 300 —— 6 容器现场里"主机启用项
        ~310 + 模板原型 ~44"按名称排序后被拦腰截断, 尾部 2 个容器的项整体
        丢失, discovered_container_count 少报成 4。截断偏袒排序头部,
        计数口径绝不允许默认丢数据, 默认提到 1000 (路由 le=1000 同口径)。
        """
        result = self._call(
            "item.get",
            {
                "output": ["itemid", "name", "key_", "lastvalue", "lastclock", "state", "error"],
                "search": {"key_": key_prefix},
                "filter": {"status": [0]},
                "selectHosts": ["host", "name"],
                "sortfield": ["name", "key_"],
                "limit": limit,
            },
            need_auth=True,
        )
        return result or []

    def fetch_docker_triggers(self, limit: int = 100) -> list[dict]:
        """Phase 7: 已实例化的 Docker 相关触发器 (模板挂接+LLD 发现后才有)。

        trigger.get 只返回真实存在的触发器, 数量本身即"模板是否挂上"的证据。
        """
        result = self._call(
            "trigger.get",
            {
                "output": ["triggerid", "description", "status", "priority"],
                "search": {"description": ["docker", "container"]},
                "selectHosts": ["host"],
                "sortfield": "description",
                "limit": limit,
            },
            need_auth=True,
        )
        return result or []


# ---------- 模块级单例 ----------

_default_client: ZabbixClient | None = None
_client_lock = threading.Lock()


def get_default_client() -> ZabbixClient:
    """按当前 Settings 构造的全局客户端 (惰性单例, 线程安全)。"""
    global _default_client
    with _client_lock:
        if _default_client is None:
            from app.config import get_settings

            settings = get_settings()
            _default_client = ZabbixClient(
                api_url=settings.zabbix_api_url,
                username=settings.zabbix_username,
                password=settings.zabbix_password,
            )
        return _default_client
