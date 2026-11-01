"""Phase 10: 给 LLM 用的 Zabbix 只读工具集 (function calling) + 执行器。

安全红线 (项目级, 写进代码结构):
- 注册表里**只有只读工具**。item.create / trigger.delete / host.update 这类
  写操作在这里根本不存在 —— 它们属于 Phase 13"人工确认执行"的范畴, 届时
  也要过审计, 绝不允许大模型直接触达。
- 本模块不直连 Zabbix API: 全部走 zabbix_client 的语义方法 (唯一出口)。
- 工具内部任何异常都消化成 {"error": "..."} 作为"观察"回传给模型,
  agent 循环绝不因单次工具失败而崩。
- 所有返回做了瘦身/截断: lastvalue 可能是 8KB 的 docker.info JSON,
  原样回传会炸 token 也炸延迟。
"""

import logging
import time
from typing import Any, Callable

from app.services.zabbix_client import ZabbixError, get_default_client

logger = logging.getLogger("zops.services.zabbix_tools")

_MAX_LIMIT = 50  # 模型想要多少都不许超过这个


def _slim(value: Any, n: int = 200) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= n else s[:n] + "...(截断)"


# ---------- 工具实现 (handler) ----------

def _list_problem_events(args: dict) -> dict:
    days = int(args.get("days", 7) or 7)
    days = max(1, min(days, 90))
    limit = min(int(args.get("limit", 20) or 20), _MAX_LIMIT)
    events = get_default_client().fetch_problem_events(
        int(time.time()) - days * 86400, limit
    )
    return {
        "count": len(events),
        "window_days": days,
        "events": [
            {
                "eventid": e.get("eventid"),
                "name": e.get("name"),
                "clock": e.get("clock"),
                "severity": e.get("severity"),
                "hosts": [h.get("name") for h in (e.get("hosts") or [])],
            }
            for e in events
        ],
    }


def _get_event_detail(args: dict) -> dict:
    eventid = str(args.get("eventid") or "").strip()
    if not eventid:
        return {"error": "参数缺失: eventid 必须是字符串"}
    e = get_default_client().fetch_event_detail(eventid)
    return {
        "eventid": e.get("eventid"),
        "name": e.get("name"),
        "clock": e.get("clock"),
        "value": e.get("value"),
        "severity": e.get("severity"),
        "r_eventid": e.get("r_eventid"),
        "hosts": e.get("hosts"),
    }


def _get_host_items(args: dict) -> dict:
    host = str(args.get("host") or "").strip()
    if not host:
        return {"error": "参数缺失: host 必须是 Zabbix Host name (如 ubuntu-host)"}
    key_search = str(args.get("key_search") or "")
    limit = min(int(args.get("limit", 30) or 30), _MAX_LIMIT)
    items = get_default_client().fetch_host_items(host, key_search, limit)
    return {
        "count": len(items),
        "items": [
            {
                "name": i.get("name"),
                "key_": _slim(i.get("key_"), 120),
                "lastvalue": _slim(i.get("lastvalue")),
                "lastclock": i.get("lastclock"),
                "state": i.get("state"),  # 0=enabled 1=unsupported(收数报错)
                "error": _slim(i.get("error"), 150),
            }
            for i in items
        ],
    }


def _get_active_triggers(args: dict) -> dict:
    host = str(args.get("host") or "").strip()
    limit = min(int(args.get("limit", 50) or 50), _MAX_LIMIT)
    triggers = get_default_client().fetch_active_triggers(host, limit)
    return {
        "count": len(triggers),
        "triggers": [
            {
                "triggerid": t.get("triggerid"),
                "description": _slim(t.get("description"), 200),
                "priority": t.get("priority"),
                "lastchange": t.get("lastchange"),
                "hosts": [h.get("name") for h in (t.get("hosts") or [])],
            }
            for t in triggers
        ],
    }


# ---------- 注册表 (name → schema + handler) ----------

_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "zbx_list_problem_events": _list_problem_events,
    "zbx_get_event_detail": _get_event_detail,
    "zbx_get_host_items": _get_host_items,
    "zbx_get_active_triggers": _get_active_triggers,
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "zbx_list_problem_events",
            "description": "列出最近 N 天 Zabbix 的 PROBLEM 事件 (eventid/名称/时间/等级/主机), 只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "回溯天数 1-90, 默认 7"},
                    "limit": {"type": "integer", "description": "最多返回条数, 上限 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "zbx_get_event_detail",
            "description": "按 eventid 查单个事件的完整原始字段 (含 r_eventid 恢复指针), 只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "eventid": {"type": "string", "description": "Zabbix eventid (字符串)"}
                },
                "required": ["eventid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "zbx_get_host_items",
            "description": "查指定主机监控项的最新值 lastvalue/采集状态 (可按 key_ 模糊过滤), 只读。lastvalue 超 200 字符会截断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Zabbix Host name, 如 ubuntu-host"},
                    "key_search": {"type": "string", "description": "监控项 key 模糊匹配, 如 docker.container_info"},
                    "limit": {"type": "integer", "description": "最多返回条数, 上限 50"},
                },
                "required": ["host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "zbx_get_active_triggers",
            "description": "列出当前处于 PROBLEM 状态 (value=1) 的触发器, 可按主机过滤, 只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "可选 Zabbix Host name"},
                    "limit": {"type": "integer", "description": "最多返回条数, 上限 50"},
                },
            },
        },
    },
]


def list_tool_names() -> list[str]:
    return sorted(_HANDLERS)


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行一次工具调用; 白名单外/参数错/Zabbix 错一律变成观察字典返回。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"工具 {name} 不存在或不在只读白名单内"}
    try:
        return handler(args or {})
    except ZabbixError as exc:
        return {"error": f"Zabbix 侧失败: {exc}"}
    except (ValueError, TypeError) as exc:
        return {"error": f"参数不合法: {exc}"}
    except Exception as exc:  # 兜底: 单次工具失败不崩 agent
        logger.exception("工具 %s 未预期异常", name)
        return {"error": f"工具执行异常: {exc}"}
