"""健康检查路由 —— 挂在根路径, 形状保持不变。

docker-compose healthcheck 与 deploy.sh 依赖 GET /health 返回
{"status": "ok", ...}, Phase 4 重构路由时特意不改动它, 避免推翻已通关的
Phase 1 基础设施。业务接口一律走 /api/v1。
"""

import logging
from typing import Any

from fastapi import APIRouter

from app.config import get_settings
from app.version import APP_VERSION

logger = logging.getLogger("zops.api.health")

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    """存活检查: 进程活着即 ok (依赖探活看 /api/v1/system/info)。"""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "env": settings.app_env,
        "version": APP_VERSION,
    }
