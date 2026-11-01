"""系统自检路由: GET /api/v1/system/info

数据库状态用真实 SELECT 1 探测, 不写死 True。
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.db.base import db_healthcheck, engine
from app.schemas.common import ApiResponse
from app.schemas.system import SystemInfo
from app.version import APP_VERSION

logger = logging.getLogger("zops.api.system")

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/info", response_model=ApiResponse[SystemInfo])
async def system_info(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[SystemInfo]:
    """返回后端自身与依赖组件的真实状态。"""
    connected = db_healthcheck()
    if not connected:
        logger.warning("system/info 自检发现数据库不可用")
    data = SystemInfo(
        service=settings.app_name,
        env=settings.app_env,
        version=APP_VERSION,
        db_dialect=engine.dialect.name,
        db_connected=connected,
        zabbix_web_url=settings.zabbix_url,
    )
    return ApiResponse(data=data)
