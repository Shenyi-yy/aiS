"""Phase 11: Docker/Linux 只读探查工具 (Agent 的第二组手)。

数据源纪律: 后端容器**没有也永远不会挂 docker.sock**。容器状态与宿主机
指标全部来自 agent2 已采集进 Zabbix 的真实数据 (Phase 7 模板 LLD 300+ 项),
本模块只是把它们组织成模型好用的形状 —— 不新开任何执行旁路。

与 zabbix_tools 同款约定:
- 只有只读工具; 写/执行类 (docker exec、system.run) 结构上不存在;
- 任何异常消化成 {"error": ...} 观察, 不崩 agent;
- 返回瘦身: 单值截断、条数封顶。
"""

import logging
import re
from typing import Any, Callable

from app.services import docker_monitor
from app.services.zabbix_client import ZabbixError, get_default_client

logger = logging.getLogger("zops.services.host_tools")

_MAX_LIMIT = 50
_STATE_KEY_PREFIX = 'docker.container_info.state.running["/'
_CONTAINER_NAME_RE = re.compile(r'\["/([^"\[\]]+)"\]')


def _slim(value: Any, n: int = 200) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= n else s[:n] + "...(截断)"


# ---------- 工具实现 ----------

def _docker_get_summary(args: dict) -> dict:
    """复用 Phase 7 聚合服务 (与 /api/v1/docker/summary 同一数据面)。"""
    s = docker_monitor.build_summary()
    return {
        "item_total": s.item_total,
        "item_unsupported": s.item_unsupported,
        "item_with_error": s.item_with_error,
        "discovered_container_count": s.discovered_container_count,
        "trigger_total": s.trigger_total,
        "per_host": [
            {
                "host": h.host,
                "item_count": h.item_count,
                "unsupported_count": h.unsupported_count,
                "error_count": h.error_count,
            }
            for h in s.per_host
        ],
    }


def _docker_list_containers(args: dict) -> dict:
    host = str(args.get("host") or "").strip()
    if not host:
        return {"error": "参数缺失: host 必须是 Zabbix Host name (如 ubuntu-host)"}
    items = get_default_client().fetch_host_items(
        host, _STATE_KEY_PREFIX, _MAX_LIMIT
    )
    containers = []
    for it in items:
        m = _CONTAINER_NAME_RE.search(str(it.get("key_", "")))
        if not m:
            continue
        containers.append(
            {
                "container": m.group(1),
                "running": str(it.get("lastvalue", "")),  # 1=运行 0=已停止
                "lastclock": it.get("lastclock"),
                "state": it.get("state"),
            }
        )
    return {"count": len(containers), "containers": containers}


def _docker_container_detail(args: dict) -> dict:
    host = str(args.get("host") or "").strip()
    name = str(args.get("container") or "").strip()
    if not host or not name:
        return {"error": "参数缺失: host 与 container (容器名, 不带 /) 都必填"}
    if _CONTAINER_NAME_RE.search(f'["/{name}"]') is None or '"' in name or "]" in name:
        return {"error": "容器名不合法 (只允许普通名字, 不要带引号/方括号)"}
    items = get_default_client().fetch_host_items(
        host, f'["/{name}"]', _MAX_LIMIT
    )
    return {
        "container": name,
        "count": len(items),
        "metrics": [
            {
                "key_": _slim(i.get("key_"), 120),
                "name": _slim(i.get("name"), 120),
                "lastvalue": _slim(i.get("lastvalue")),
                "lastclock": i.get("lastclock"),
                "state": i.get("state"),
                "error": _slim(i.get("error"), 150),
            }
            for i in items
        ],
    }


def _host_get_linux_metrics(args: dict) -> dict:
    host = str(args.get("host") or "").strip()
    if not host:
        return {"error": "参数缺失: host 必须是 Zabbix Host name (如 ubuntu-host)"}
    client = get_default_client()
    keyword = str(args.get("keyword") or "").strip()
    prefixes = [keyword] if keyword else ["system.", "vm.", "vfs."]
    merged: list[dict] = []
    seen: set[str] = set()
    for prefix in prefixes:
        per_limit = _MAX_LIMIT if keyword else 15
        try:
            items = client.fetch_host_items(host, prefix, per_limit)
        except ZabbixError as exc:
            return {"error": f"Zabbix 侧失败: {exc}"}
        for i in items:
            key = str(i.get("key_", ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "key_": _slim(key, 120),
                    "name": _slim(i.get("name"), 120),
                    "lastvalue": _slim(i.get("lastvalue")),
                    "lastclock": i.get("lastclock"),
                    "state": i.get("state"),
                }
            )
    return {"count": len(merged), "items": merged}


# ---------- 注册表 ----------

_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "docker_get_summary": _docker_get_summary,
    "docker_list_containers": _docker_list_containers,
    "docker_container_detail": _docker_container_detail,
    "host_get_linux_metrics": _host_get_linux_metrics,
}

HOST_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "docker_get_summary",
            "description": "Docker 监控覆盖与健康度汇总 (监控项总数/unsupported/error/触发器数, 数据来自 agent2→Zabbix 实采), 只读, 无需参数。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docker_list_containers",
            "description": "列出该主机 Zabbix 已发现的全部容器及运行状态 (running=1/0), 只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Zabbix Host name, 如 ubuntu-host"},
                },
                "required": ["host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docker_container_detail",
            "description": "查单个容器的全部已采集监控项最新值 (state/network/cpu/restart count 等, 最多50项, 值截断200字符), 只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Zabbix Host name"},
                    "container": {"type": "string", "description": "容器名, 不带斜杠, 如 zops-drill"},
                },
                "required": ["host", "container"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "host_get_linux_metrics",
            "description": "查宿主机 Linux 指标最新值 (CPU/内存/磁盘/负载等 system./vm./vfs. 项; 可用 keyword 精确过滤如 system.cpu)。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Zabbix Host name"},
                    "keyword": {"type": "string", "description": "可选 key_ 模糊过滤前缀"},
                },
                "required": ["host"],
            },
        },
    },
]


def list_tool_names() -> list[str]:
    return sorted(_HANDLERS)


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行 host 组工具; 白名单外/参数错/Zabbix 错一律变观察字典。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"工具 {name} 不存在或不在只读白名单内"}
    try:
        return handler(args or {})
    except ZabbixError as exc:
        return {"error": f"Zabbix 侧失败: {exc}"}
    except (ValueError, TypeError) as exc:
        return {"error": f"参数不合法: {exc}"}
    except Exception as exc:
        logger.exception("host 工具 %s 未预期异常", name)
        return {"error": f"工具执行异常: {exc}"}
