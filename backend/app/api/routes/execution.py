"""执行审批路由: /api/v1/exec/* (Phase 13 人工确认 + 安全执行)

- GET  /config                执行通道状态 (开关/白名单/socket 存在性, 全真实)
- GET  /operations            可执行操作目录回显 (安全证据: 静态白名单)
- GET  /requests              审批单分页 (可按状态/问题过滤, AUTO+MANUAL 都在)
- POST /requests              人工创建审批单 (只会产出 PENDING_APPROVAL)
- GET  /requests/{id}         单据详情
- POST /requests/{id}/approve 人工批准 = 全系统唯一执行入口 (留名, 可附意见)
- POST /requests/{id}/reject  人工否决 (终态, 后台不再对同问题同操作重复建议)

状态机语义在 HTTP 层的映射: 不存在=404 / 非待审批再操作=409 /
白名单不含目标=409 / EXEC_ENABLED=false=503 / 引擎失败单据落 FAILED 返 502。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import ExecutionRecord
from app.schemas.common import ApiResponse
from app.schemas.execution import (
    ApproveIn,
    ExecConfigOut,
    ExecutionCreateIn,
    ExecutionOut,
    ExecutionPage,
    OperationsList,
    OperationOut,
    RejectIn,
)
from app.services import docker_exec, exec_catalog
from app.services.execution import (
    ExecConflict,
    ExecDisabled,
    ExecNotFound,
    approve_request,
    create_request,
    get_request,
    pending_requests,
    reject_request,
)

logger = logging.getLogger("zops.api.execution")

router = APIRouter(prefix="/exec", tags=["exec"])


@router.get("/config", response_model=ApiResponse[ExecConfigOut])
def exec_config(db: Session = Depends(get_db)) -> ApiResponse[ExecConfigOut]:
    settings = get_settings()
    return ApiResponse(
        data=ExecConfigOut(
            enabled=settings.exec_enabled,
            auto_suggest=settings.exec_auto_suggest,
            docker_socket=settings.exec_docker_socket,
            socket_exists=docker_exec.socket_available(),
            allow_targets=exec_catalog.allowed_targets(),
            timeout_seconds=settings.exec_timeout,
            approval_required=True,  # 结构事实: 不存在免审批代码路径
            pending_requests=pending_requests(db),
        )
    )


@router.get("/operations", response_model=ApiResponse[OperationsList])
def list_operations() -> ApiResponse[OperationsList]:
    """目录回显: target 全在 EXEC_ALLOW_TARGETS 内, 动作全在固定三集合内。"""
    return ApiResponse(
        data=OperationsList(
            allowed_actions=sorted(exec_catalog.ALLOWED_ACTIONS),
            operations=[
                OperationOut(
                    op_id=op.op_id,
                    name=op.name,
                    action=op.action,
                    target=op.target,
                    description=op.description,
                    risk=op.risk,
                )
                for op in exec_catalog.visible_operations()
            ],
        )
    )


@router.get("/requests", response_model=ApiResponse[ExecutionPage])
def list_requests(
    db: Session = Depends(get_db),
    status: str | None = Query(
        default=None,
        pattern="^(PENDING_APPROVAL|EXECUTED|FAILED|REJECTED)$",
    ),
    problem_record_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[ExecutionPage]:
    stmt = select(ExecutionRecord)
    if status is not None:
        stmt = stmt.where(ExecutionRecord.status == status)
    if problem_record_id is not None:
        stmt = stmt.where(ExecutionRecord.problem_record_id == problem_record_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(ExecutionRecord.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return ApiResponse(
        data=ExecutionPage(
            total=total,
            offset=offset,
            limit=limit,
            items=[ExecutionOut.model_validate(r) for r in rows],
        )
    )


@router.get("/requests/{request_id}", response_model=ApiResponse[ExecutionOut])
def read_request(
    request_id: int, db: Session = Depends(get_db)
) -> ApiResponse[ExecutionOut]:
    try:
        req = get_request(db, request_id)
    except ExecNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=ExecutionOut.model_validate(req))


@router.post("/requests", response_model=ApiResponse[ExecutionOut])
def create(
    body: ExecutionCreateIn, db: Session = Depends(get_db)
) -> ApiResponse[ExecutionOut]:
    """人工发起: 得到的只是 PENDING_APPROVAL 单据 —— 本接口零执行。"""
    try:
        req = create_request(
            db,
            body.op_id,
            problem_record_id=body.problem_record_id,
            source="MANUAL",
            proposal=(
                f"手工申请执行目录操作 {body.op_id}"
                + (f": {body.comment}" if body.comment else "")
            ),
        )
    except ExecNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=ExecutionOut.model_validate(req))


@router.post("/requests/{request_id}/approve", response_model=ApiResponse[ExecutionOut])
def approve(
    request_id: int,
    body: ApproveIn,
    db: Session = Depends(get_db),
) -> ApiResponse[ExecutionOut]:
    """批准并同步执行 (Docker API 秒级往返)。执行失败单据落 FAILED 并返 502,
    但审批人/意见/失败原因已经持久化 —— 失败也要留下完整证据链。"""
    try:
        req = approve_request(db, request_id, body.approver, body.comment)
    except ExecNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExecDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if req.status == "FAILED":
        raise HTTPException(
            status_code=502,
            detail=f"已批准但执行失败 (单据 {req.id} 落 FAILED): {req.error}",
        )
    return ApiResponse(data=ExecutionOut.model_validate(req))


@router.post("/requests/{request_id}/reject", response_model=ApiResponse[ExecutionOut])
def reject(
    request_id: int,
    body: RejectIn,
    db: Session = Depends(get_db),
) -> ApiResponse[ExecutionOut]:
    try:
        req = reject_request(db, request_id, body.approver, body.reason)
    except ExecNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=ExecutionOut.model_validate(req))
