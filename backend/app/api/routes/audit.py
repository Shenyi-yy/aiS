"""审计路由: /api/v1/audit* (Phase 14)

- GET /api/v1/audit                    事件分页 (可按类型/来源/问题过滤)
- GET /api/v1/audit/verify             哈希链全量重放校验
- GET /api/v1/audit/timeline/problem/{problem_id}  单问题全链证据

**结构约束: 本文件只有 GET 路由, 没有任何 PUT/PATCH/DELETE/POST。**
只追加不是口号, 是 openapi.json 里可以逐字核对的事实 (验证脚本 [4/6]
据此对质)。服务层同样不存在改/删函数, 双层收口。
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import AIAnalysisRecord, AuditEvent, ExecutionRecord, ProblemRecord
from app.schemas.audit import AuditEventOut, AuditPage, ChainVerifyOut, TimelineOut
from app.schemas.common import ApiResponse
from app.services.audit import verify_chain

logger = logging.getLogger("zops.api.audit")

router = APIRouter(prefix="/audit", tags=["audit"])


def _row_brief(row: object, fields: list[str]) -> dict[str, object]:
    """ORM 行 → 精简 dict (datetime 转 isoformat, 供时间线出参)。"""
    out: dict[str, object] = {}
    for f in fields:
        v = getattr(row, f, None)
        out[f] = v.isoformat() if isinstance(v, datetime) else v
    return out


@router.get("", response_model=ApiResponse[AuditPage])
def list_events(
    db: Session = Depends(get_db),
    event_type: str | None = Query(default=None, max_length=48, description="按事件类型精确过滤"),
    source: str | None = Query(default=None, pattern="^(API|AUTO|SYSTEM)$", description="按触发面过滤"),
    link_problem_id: int | None = Query(default=None, ge=1, description="按关联归档问题过滤"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[AuditPage]:
    """分页查询审计事件, 按 id 倒序 (最新在前); 哈希字段原样带出。"""
    stmt = select(AuditEvent)
    if event_type is not None:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if source is not None:
        stmt = stmt.where(AuditEvent.source == source)
    if link_problem_id is not None:
        stmt = stmt.where(AuditEvent.link_problem_id == link_problem_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AuditEvent.id.desc()).offset(offset).limit(limit)
    ).all()
    return ApiResponse(
        data=AuditPage(
            total=total, offset=offset, limit=limit,
            items=[AuditEventOut.model_validate(r) for r in rows],
        )
    )


@router.get("/verify", response_model=ApiResponse[ChainVerifyOut])
def chain_verify(db: Session = Depends(get_db)) -> ApiResponse[ChainVerifyOut]:
    """全量重放校验哈希链 —— 断链报告第一个出问题行的 id 与原因。"""
    return ApiResponse(data=ChainVerifyOut(**verify_chain(db)))


@router.get("/timeline/problem/{problem_id}", response_model=ApiResponse[TimelineOut])
def problem_timeline(
    problem_id: int,
    db: Session = Depends(get_db),
) -> ApiResponse[TimelineOut]:
    """单问题全链证据: 归档 → 分析(证据快照/轨迹/来源) → 执行单据 → 审计事件。"""
    problem = db.get(ProblemRecord, problem_id)
    if problem is None:
        raise HTTPException(
            status_code=404,
            detail=f"problem_records 没有 id={problem_id} —— 时间线只组装真实归档, 不补假数据",
        )
    analyses = db.scalars(
        select(AIAnalysisRecord)
        .where(AIAnalysisRecord.problem_record_id == problem_id)
        .order_by(AIAnalysisRecord.id.asc())
    ).all()
    executions = db.scalars(
        select(ExecutionRecord)
        .where(ExecutionRecord.problem_record_id == problem_id)
        .order_by(ExecutionRecord.id.asc())
    ).all()
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.link_problem_id == problem_id)
        .order_by(AuditEvent.id.asc())
    ).all()
    return ApiResponse(
        data=TimelineOut(
            problem=_row_brief(
                problem,
                ["id", "zabbix_eventid", "host", "trigger_name", "severity",
                 "status", "started_at", "recovered_at"],
            ),
            analyses=[
                _row_brief(
                    a,
                    ["id", "status", "model", "trigger_source", "analysis",
                     "error", "tool_trace", "elapsed_ms", "created_at"],
                )
                for a in analyses
            ],
            executions=[
                _row_brief(
                    e,
                    ["id", "op_id", "action", "target", "source", "status",
                     "proposal", "approver", "comment", "result", "error",
                     "elapsed_ms", "decided_at", "created_at"],
                )
                for e in executions
            ],
            events=[AuditEventOut.model_validate(ev) for ev in events],
        )
    )
