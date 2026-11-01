"""问题归档路由: /api/v1/problems

- GET  /          分页查询归档 (数据来自真实 Zabbix 事件)
- POST /sync      立即执行一次 Zabbix → problem_records 同步

同步逻辑集中在 services/problem_sync + services/zabbix_client,
路由层不出现任何 Zabbix API 细节。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import ProblemRecord
from app.schemas.common import ApiResponse
from app.schemas.problem import ProblemOut, ProblemPage, SyncResult
from app.services.problem_sync import sync_once
from app.services.zabbix_client import ZabbixError

logger = logging.getLogger("zops.api.problems")

router = APIRouter(prefix="/problems", tags=["problems"])


@router.post("/sync", response_model=ApiResponse[SyncResult])
def trigger_sync(
    days: int | None = Query(
        default=None, ge=1, le=365, description="回溯天数, 默认取 ZABBIX_SYNC_DAYS"
    ),
) -> ApiResponse[SyncResult]:
    """手动触发一次归档同步 (验证脚本与前端刷新按钮共用)。"""
    try:
        result = sync_once(days=days)
    except ZabbixError as exc:
        logger.warning("手动同步失败: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApiResponse(data=result)


@router.get("", response_model=ApiResponse[ProblemPage])
async def list_problems(
    db: Session = Depends(get_db),
    status: str | None = Query(
        default=None,
        pattern="^(ACTIVE|RECOVERED)$",
        description="按状态过滤; 不传则返回全部",
    ),
    severity: int | None = Query(default=None, ge=0, le=5, description="Zabbix 告警等级"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[ProblemPage]:
    """分页查询问题归档, 按发生时间倒序。"""
    stmt = select(ProblemRecord)
    if status is not None:
        stmt = stmt.where(ProblemRecord.status == status)
    if severity is not None:
        stmt = stmt.where(ProblemRecord.severity == severity)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(ProblemRecord.started_at.desc()).offset(offset).limit(limit)
    ).all()

    page = ProblemPage(
        total=total,
        offset=offset,
        limit=limit,
        items=[ProblemOut.model_validate(r) for r in rows],
    )
    return ApiResponse(data=page)
