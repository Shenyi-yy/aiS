"""Docker 监控路由: /api/v1/docker/summary

数据实时来自 Zabbix API (item.get / trigger.get), 经 services/docker_monitor
聚合; API 细节全部封在 zabbix_client 唯一出口里, 本层不出现 JSON-RPC 参数。
"""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.schemas.common import ApiResponse
from app.schemas.docker import DockerSummary
from app.services.docker_monitor import build_summary
from app.services.zabbix_client import ZabbixError

logger = logging.getLogger("zops.api.docker")

router = APIRouter(prefix="/docker", tags=["docker"])


@router.get("/summary", response_model=ApiResponse[DockerSummary])
def docker_summary(
    item_limit: int = Query(default=1000, ge=1, le=1000,
                            description="最多返回多少条 docker.* 监控项 (计数为全量)"),
) -> ApiResponse[DockerSummary]:
    """Docker 监控覆盖概览: 真实 item/trigger 计数 + 收数健康度。"""
    try:
        summary = build_summary(item_limit=item_limit)
    except ZabbixError as exc:
        logger.warning("docker/summary 失败: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApiResponse(data=summary)
