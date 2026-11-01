"""Zabbix API 状态路由: /api/v1/zabbix/*

路由层不直接写 JSON-RPC —— 全部经 services/zabbix_client 语义方法。
处理器用 sync def: FastAPI 自动放线程池, 不会卡住事件循环。
"""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.schemas.common import ApiResponse
from app.schemas.zabbix import ZabbixHealth
from app.services.zabbix_client import ZabbixError, get_default_client

logger = logging.getLogger("zops.api.zabbix")

router = APIRouter(prefix="/zabbix", tags=["zabbix"])


@router.get("/health", response_model=ApiResponse[ZabbixHealth])
def zabbix_health() -> ApiResponse[ZabbixHealth]:
    """登录 Zabbix API 并读取版本号 (凭据错误返回 502, 不含密码)。"""
    try:
        version = get_default_client().health()
    except ZabbixError as exc:
        logger.warning("Zabbix API 健康检查失败: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApiResponse(data=ZabbixHealth(connected=True, api_version=version))


@router.get("/items", response_model=ApiResponse[dict])
def zabbix_item_diagnostics(
    host_name: str = Query(..., description="主机名, 如 ubuntu-host"),
    key: str = Query(..., description="item key, 支持模糊匹配后精确过滤"),
) -> ApiResponse[dict]:
    """诊断某个 item 是否真的在收数 (返回 Zabbix 原始字段, 不做字段假设)。

    Phase 3 遗留: vfs.fs.size[/,pused] 手动 item 落库 0 行, Phase 5 起用它查
    lastvalue/lastclock/status 定位真实原因。
    """
    try:
        items = get_default_client().fetch_items_by_key(key)
    except ZabbixError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    matched = [
        it
        for it in items
        if it.get("key_") == key
        and any(h.get("name") == host_name for h in it.get("hosts") or [])
    ]
    return ApiResponse(
        data={
            "query": {"host": host_name, "key": key},
            "matched": len(matched),
            "items": matched,
        }
    )
