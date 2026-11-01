"""主机列表路由: GET /api/v1/hosts

数据实时来自 Zabbix API host.get (经唯一客户端出口), 不落本地库、不造假。
"""

import logging

from fastapi import APIRouter, HTTPException

from app.schemas.common import ApiResponse
from app.schemas.host import HostInfo
from app.services.zabbix_client import ZabbixError, get_default_client

logger = logging.getLogger("zops.api.hosts")

router = APIRouter(prefix="/hosts", tags=["hosts"])


@router.get("", response_model=ApiResponse[list[HostInfo]])
def list_hosts() -> ApiResponse[list[HostInfo]]:
    """受监控主机清单 (Zabbix status=0)。"""
    try:
        raw = get_default_client().fetch_hosts()
    except ZabbixError as exc:
        logger.warning("host.get 失败: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = [
        HostInfo(
            hostid=int(h["hostid"]),
            technical_name=str(h["host"]),
            name=str(h["name"] or h["host"]),
            monitored=str(h["status"]) == "0",
        )
        for h in raw
    ]
    return ApiResponse(data=items)
